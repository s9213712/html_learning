#!/usr/bin/env python3
"""Versioned resource evidence and hard-stop evaluation for campaign runs."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


class _LazyRequests:
    """Keep dormant admission free of third-party imports until HTTP is used."""

    @staticmethod
    def get(*args, **kwargs):
        from requests import get

        return get(*args, **kwargs)


requests = _LazyRequests()


RESOURCE_SAMPLE_SCHEMA_VERSION = "hackme.resource-sample.v1"
HOST_SAFETY_SCHEMA_VERSION = "hackme.host-safety-preflight.v1"
MIB = 1024**2
GIB = 1024**3
DEFAULT_MINIMUM_HOST_MEM_AVAILABLE_BYTES = 3 * GIB
DEFAULT_MAXIMUM_HOST_LOAD1_PER_CPU = 1.0
DEFAULT_MAXIMUM_HOST_CPU_PRESSURE_SOME_AVG10 = 80.0
DEFAULT_MAXIMUM_HOST_MEMORY_PRESSURE_FULL_AVG10 = 5.0
DEFAULT_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG10 = 10.0
DEFAULT_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG60 = 10.0
STARTUP_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG10 = 3.0
STARTUP_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG60 = 3.0
DEFAULT_MAXIMUM_HOST_SWAP_USED_BYTES = 512 * MIB
STARTUP_BLOCK_IO_MINIMUM_WINDOW_SECONDS = 4.0
STARTUP_BLOCK_IO_MAXIMUM_WINDOW_SECONDS = 7.5
STARTUP_BLOCK_IO_MAXIMUM_READ_AWAIT_MS = 60.0
STARTUP_BLOCK_IO_MAXIMUM_WRITE_AWAIT_MS = 60.0
STARTUP_BLOCK_IO_MAXIMUM_FLUSH_AWAIT_MS = 100.0
STARTUP_BLOCK_IO_MAXIMUM_AVERAGE_QUEUE_DEPTH = 0.25
WAITABLE_HOST_SAFETY_REASONS = frozenset({
    "HOST_IO_PRESSURE_HIGH",
    "HOST_BLOCK_DEVICE_NOT_QUIET",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _read_int(path: Path) -> int:
    value = _read_text(path)
    if value == "max":
        return -1
    return int(value)


def _key_value_lines(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                result[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return result


def _meminfo(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in _read_text(path).splitlines():
        if ":" not in line:
            continue
        name, raw = line.split(":", 1)
        parts = raw.strip().split()
        if not parts:
            continue
        multiplier = 1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1
        values[name] = int(parts[0]) * multiplier
    return values


def _parse_psi(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in _read_text(path).splitlines():
        parts = line.split()
        if not parts:
            continue
        row: dict[str, Any] = {}
        for item in parts[1:]:
            if "=" not in item:
                continue
            key, raw = item.split("=", 1)
            row[key] = int(raw) if key == "total" else float(raw)
        result[parts[0]] = row
    return result


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float(value) == float(value)
        and float(value) not in {float("inf"), float("-inf")}
    )


def _host_safety_checks(
    host: Mapping[str, Any],
    *,
    minimum_host_mem_available_bytes: int,
    maximum_host_load1_per_cpu: float,
    maximum_host_cpu_pressure_some_avg10: float,
    maximum_host_memory_pressure_full_avg10: float,
    maximum_host_io_pressure_full_avg10: float,
    maximum_host_io_pressure_full_avg60: float,
    maximum_host_swap_used_bytes: int,
) -> dict[str, dict[str, Any]]:
    load1 = _nested_get(host, "load.load1")
    memory_available = _nested_get(host, "memory.available_bytes")
    cpu_pressure = _nested_get(host, "psi.cpu.some.avg10")
    memory_pressure = _nested_get(host, "psi.memory.full.avg10")
    io_pressure = _nested_get(host, "psi.io.full.avg10")
    io_pressure_avg60 = _nested_get(host, "psi.io.full.avg60")
    swap_used = _nested_get(host, "swap.used_bytes")
    cpu_count = max(1, int(os.cpu_count() or 1))
    maximum_load1 = float(maximum_host_load1_per_cpu) * cpu_count

    return {
        "host_mem_available": {
            "ok": _finite_number(memory_available)
            and int(memory_available) >= int(minimum_host_mem_available_bytes),
            "value": memory_available,
            "minimum": int(minimum_host_mem_available_bytes),
            "reason_code": "HOST_MEMORY_AVAILABLE_LOW",
        },
        "host_load1": {
            "ok": _finite_number(load1) and float(load1) <= maximum_load1,
            "value": load1,
            "maximum": maximum_load1,
            "cpu_count": cpu_count,
            "reason_code": "HOST_LOAD1_HIGH",
        },
        "host_cpu_pressure": {
            "ok": _finite_number(cpu_pressure)
            and float(cpu_pressure) <= float(maximum_host_cpu_pressure_some_avg10),
            "value": cpu_pressure,
            "maximum": float(maximum_host_cpu_pressure_some_avg10),
            "reason_code": "HOST_CPU_PRESSURE_HIGH",
        },
        "host_memory_pressure": {
            "ok": _finite_number(memory_pressure)
            and float(memory_pressure) <= float(maximum_host_memory_pressure_full_avg10),
            "value": memory_pressure,
            "maximum": float(maximum_host_memory_pressure_full_avg10),
            "reason_code": "HOST_MEMORY_PRESSURE_HIGH",
        },
        "host_io_pressure": {
            "ok": _finite_number(io_pressure)
            and _finite_number(io_pressure_avg60)
            and float(io_pressure) <= float(maximum_host_io_pressure_full_avg10)
            and float(io_pressure_avg60) <= float(maximum_host_io_pressure_full_avg60),
            "value": {"avg10": io_pressure, "avg60": io_pressure_avg60},
            "maximum": {
                "avg10": float(maximum_host_io_pressure_full_avg10),
                "avg60": float(maximum_host_io_pressure_full_avg60),
            },
            "reason_code": "HOST_IO_PRESSURE_HIGH",
        },
        "host_swap_used": {
            "ok": _finite_number(swap_used)
            and int(swap_used) <= int(maximum_host_swap_used_bytes),
            "value": swap_used,
            "maximum": int(maximum_host_swap_used_bytes),
            "reason_code": "HOST_SWAP_USAGE_HIGH",
        },
    }


def collect_host_safety_preflight(
    *,
    proc_root: Path = Path("/proc"),
    minimum_host_mem_available_bytes: int = DEFAULT_MINIMUM_HOST_MEM_AVAILABLE_BYTES,
    maximum_host_load1_per_cpu: float = DEFAULT_MAXIMUM_HOST_LOAD1_PER_CPU,
    maximum_host_cpu_pressure_some_avg10: float = DEFAULT_MAXIMUM_HOST_CPU_PRESSURE_SOME_AVG10,
    maximum_host_memory_pressure_full_avg10: float = DEFAULT_MAXIMUM_HOST_MEMORY_PRESSURE_FULL_AVG10,
    maximum_host_io_pressure_full_avg10: float = DEFAULT_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG10,
    maximum_host_io_pressure_full_avg60: float = DEFAULT_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG60,
    maximum_host_swap_used_bytes: int = DEFAULT_MAXIMUM_HOST_SWAP_USED_BYTES,
) -> dict[str, Any]:
    """Read only small procfs files and fail closed before campaign startup."""

    proc_root = Path(proc_root)
    errors: dict[str, str] = {}
    host: dict[str, Any] = {}
    try:
        load = _read_text(proc_root / "loadavg").split()
        host["load"] = {
            "load1": float(load[0]),
            "load5": float(load[1]),
            "load15": float(load[2]),
        }
    except Exception as exc:
        errors["host.load"] = f"{exc.__class__.__name__}: {exc}"
        host["load"] = {}
    try:
        mem = _meminfo(proc_root / "meminfo")
        swap_total = int(mem.get("SwapTotal", 0))
        swap_free = int(mem.get("SwapFree", 0))
        host["memory"] = {
            "available_bytes": int(mem.get("MemAvailable", 0)),
            "total_bytes": int(mem.get("MemTotal", 0)),
        }
        host["swap"] = {
            "used_bytes": max(0, swap_total - swap_free),
            "total_bytes": swap_total,
        }
    except Exception as exc:
        errors["host.memory"] = f"{exc.__class__.__name__}: {exc}"
        host["memory"] = {}
        host["swap"] = {}
    for name in ("cpu", "memory", "io"):
        try:
            host.setdefault("psi", {})[name] = _parse_psi(
                proc_root / "pressure" / name
            )
        except Exception as exc:
            errors[f"host.psi.{name}"] = f"{exc.__class__.__name__}: {exc}"
            host.setdefault("psi", {})[name] = {}

    checks = _host_safety_checks(
        host,
        minimum_host_mem_available_bytes=minimum_host_mem_available_bytes,
        maximum_host_load1_per_cpu=maximum_host_load1_per_cpu,
        maximum_host_cpu_pressure_some_avg10=maximum_host_cpu_pressure_some_avg10,
        maximum_host_memory_pressure_full_avg10=maximum_host_memory_pressure_full_avg10,
        maximum_host_io_pressure_full_avg10=maximum_host_io_pressure_full_avg10,
        maximum_host_io_pressure_full_avg60=maximum_host_io_pressure_full_avg60,
        maximum_host_swap_used_bytes=maximum_host_swap_used_bytes,
    )
    tripped = [row["reason_code"] for row in checks.values() if not row["ok"]]
    if errors:
        tripped.append("HOST_SAFETY_TELEMETRY_INCOMPLETE")
    return {
        "schema_version": HOST_SAFETY_SCHEMA_VERSION,
        "at": utc_now(),
        "host": host,
        "checks": checks,
        "errors": errors,
        "tripped": tripped,
        "ok": not tripped,
    }


def _with_host_io_hard_limit(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the non-waitable 10/10 ceiling from raw PSI evidence."""

    result = dict(evidence)
    checks = dict(result.get("checks") or {})
    io_check = checks.get("host_io_pressure") or {}
    value = io_check.get("value") or {}
    avg10 = value.get("avg10")
    avg60 = value.get("avg60")
    hard_limit_evaluated = _finite_number(avg10) and _finite_number(avg60)
    hard_limit_exceeded = (
        (
            float(avg10) > DEFAULT_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG10
            or float(avg60) > DEFAULT_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG60
        )
        if hard_limit_evaluated
        else None
    )
    checks["host_io_pressure_hard_limit"] = {
        "ok": bool(hard_limit_evaluated and not hard_limit_exceeded),
        "evaluated": hard_limit_evaluated,
        "exceeded": hard_limit_exceeded,
        "value": {"avg10": avg10, "avg60": avg60},
        "maximum": {
            "avg10": DEFAULT_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG10,
            "avg60": DEFAULT_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG60,
        },
        "reason_code": "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
    }
    result["checks"] = checks
    if hard_limit_exceeded is True:
        result["tripped"] = list(dict.fromkeys([
            *(result.get("tripped") or []),
            "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
        ]))
        result["ok"] = False
    return result


def collect_host_startup_safety_preflight(
    *,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    """Require cold-start I/O headroom without tightening runtime hard stops."""

    return _with_host_io_hard_limit(collect_host_safety_preflight(
        proc_root=proc_root,
        maximum_host_io_pressure_full_avg10=(
            STARTUP_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG10
        ),
        maximum_host_io_pressure_full_avg60=(
            STARTUP_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG60
        ),
    ))


class HostStartupBlockIoSampler:
    """Stateful, read-only startup gate for delayed block-device pressure."""

    def __init__(
        self,
        *,
        data_root: Path = Path("/"),
        proc_root: Path = Path("/proc"),
        device_major_minor: tuple[int, int] | None = None,
        read_text: Callable[[Path], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.proc_root = Path(proc_root)
        self._device_major_minor = device_major_minor
        self._read_text = read_text or _read_text
        self._clock = clock or time.monotonic
        self._history: list[dict[str, Any]] = []

    def _resolve_device(self) -> tuple[int, int]:
        if self._device_major_minor is None:
            device = os.stat(self.data_root).st_dev
            self._device_major_minor = (os.major(device), os.minor(device))
        return self._device_major_minor

    def _snapshot(self) -> dict[str, Any]:
        wanted = self._resolve_device()
        for line in self._read_text(self.proc_root / "diskstats").splitlines():
            fields = line.split()
            if len(fields) < 20:
                continue
            if (int(fields[0]), int(fields[1])) != wanted:
                continue
            return {
                "monotonic": float(self._clock()),
                "major": wanted[0],
                "minor": wanted[1],
                "device": fields[2],
                "reads": int(fields[3]),
                "read_ms": int(fields[6]),
                "writes": int(fields[7]),
                "write_ms": int(fields[10]),
                "in_flight": int(fields[11]),
                "weighted_io_ms": int(fields[13]),
                "flushes": int(fields[18]),
                "flush_ms": int(fields[19]),
            }
        raise RuntimeError("whole block device diskstats record unavailable")

    @staticmethod
    def _pending(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": "pending",
            "safe": False,
            "reason_codes": ["block_io_baseline_pending"],
            "device_major_minor": (
                f"{int(snapshot['major'])}:{int(snapshot['minor'])}"
            ),
            "device": snapshot.get("device"),
            "interval_seconds": None,
            "metrics": {},
        }

    def _evaluate(self, current: dict[str, Any]) -> dict[str, Any]:
        if self._history and (
            self._history[-1].get("major"),
            self._history[-1].get("minor"),
            self._history[-1].get("device"),
        ) != (
            current.get("major"),
            current.get("minor"),
            current.get("device"),
        ):
            self._history = [current]
            return {
                **self._pending(current),
                "status": "unavailable",
                "reason_codes": ["block_io_device_changed"],
            }
        self._history.append(current)
        while (
            len(self._history) > 1
            and current["monotonic"] - self._history[1]["monotonic"]
            >= STARTUP_BLOCK_IO_MINIMUM_WINDOW_SECONDS
        ):
            self._history.pop(0)
        previous = self._history[0]
        elapsed_seconds = current["monotonic"] - previous["monotonic"]
        if elapsed_seconds < STARTUP_BLOCK_IO_MINIMUM_WINDOW_SECONDS:
            return self._pending(current)
        if elapsed_seconds > STARTUP_BLOCK_IO_MAXIMUM_WINDOW_SECONDS:
            self._history = [current]
            return self._pending(current)

        counter_names = (
            "reads",
            "read_ms",
            "writes",
            "write_ms",
            "weighted_io_ms",
            "flushes",
            "flush_ms",
        )
        if any(current[name] < previous[name] for name in counter_names):
            self._history = [current]
            return {
                **self._pending(current),
                "status": "unavailable",
                "reason_codes": ["block_io_counter_reset"],
            }

        completed_reads = current["reads"] - previous["reads"]
        read_milliseconds = current["read_ms"] - previous["read_ms"]
        completed_writes = current["writes"] - previous["writes"]
        write_milliseconds = current["write_ms"] - previous["write_ms"]
        completed_flushes = current["flushes"] - previous["flushes"]
        flush_milliseconds = current["flush_ms"] - previous["flush_ms"]
        weighted_milliseconds = (
            current["weighted_io_ms"] - previous["weighted_io_ms"]
        )
        if (
            (completed_reads == 0 and read_milliseconds != 0)
            or (completed_writes == 0 and write_milliseconds != 0)
            or (completed_flushes == 0 and flush_milliseconds != 0)
        ):
            self._history = [current]
            return {
                **self._pending(current),
                "status": "unavailable",
                "reason_codes": ["block_io_counter_inconsistent"],
            }

        read_await_ms = (
            read_milliseconds / completed_reads if completed_reads else 0.0
        )
        write_await_ms = (
            write_milliseconds / completed_writes if completed_writes else 0.0
        )
        flush_await_ms = (
            flush_milliseconds / completed_flushes
            if completed_flushes
            else 0.0
        )
        average_queue_depth = weighted_milliseconds / (elapsed_seconds * 1000.0)
        metrics = {
            "in_flight_start": int(previous["in_flight"]),
            "in_flight_end": int(current["in_flight"]),
            "read_await_ms": round(read_await_ms, 6),
            "write_await_ms": round(write_await_ms, 6),
            "flush_await_ms": round(flush_await_ms, 6),
            "average_queue_depth": round(average_queue_depth, 6),
        }
        reason_codes: list[str] = []
        if previous["in_flight"] != 0 or current["in_flight"] != 0:
            reason_codes.append("block_io_in_flight_nonzero")
        if read_await_ms > STARTUP_BLOCK_IO_MAXIMUM_READ_AWAIT_MS:
            reason_codes.append("block_io_read_await_high")
        if write_await_ms > STARTUP_BLOCK_IO_MAXIMUM_WRITE_AWAIT_MS:
            reason_codes.append("block_io_write_await_high")
        if flush_await_ms > STARTUP_BLOCK_IO_MAXIMUM_FLUSH_AWAIT_MS:
            reason_codes.append("block_io_flush_await_high")
        if average_queue_depth > STARTUP_BLOCK_IO_MAXIMUM_AVERAGE_QUEUE_DEPTH:
            reason_codes.append("block_io_average_queue_high")
        return {
            "status": "safe" if not reason_codes else "unsafe",
            "safe": not reason_codes,
            "reason_codes": reason_codes,
            "device_major_minor": f"{current['major']}:{current['minor']}",
            "device": current["device"],
            "interval_seconds": round(elapsed_seconds, 6),
            "metrics": metrics,
            "maximum": {
                "read_await_ms": STARTUP_BLOCK_IO_MAXIMUM_READ_AWAIT_MS,
                "write_await_ms": STARTUP_BLOCK_IO_MAXIMUM_WRITE_AWAIT_MS,
                "flush_await_ms": STARTUP_BLOCK_IO_MAXIMUM_FLUSH_AWAIT_MS,
                "average_queue_depth": (
                    STARTUP_BLOCK_IO_MAXIMUM_AVERAGE_QUEUE_DEPTH
                ),
            },
        }

    def sample(self) -> dict[str, Any]:
        try:
            return self._evaluate(self._snapshot())
        except Exception as exc:
            self._history.clear()
            return {
                "status": "unavailable",
                "safe": False,
                "reason_codes": ["block_io_telemetry_unavailable"],
                "device_major_minor": None,
                "device": None,
                "interval_seconds": None,
                "metrics": {},
                "error_type": exc.__class__.__name__,
            }


def wait_for_host_safety_preflight(
    *,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 1.0,
    required_consecutive_safe: int = 2,
    collector: Callable[[], dict[str, Any]] = collect_host_safety_preflight,
    block_io_sampler: HostStartupBlockIoSampler | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait only for transient I/O pressure before any campaign work begins.

    The large runner module can briefly fault source pages into memory.  This
    admission wait performs no repository scan or server action and never
    relaxes a threshold.  All non-I/O failures remain immediate fail-closed.
    """

    if timeout_seconds < 0 or poll_seconds <= 0 or required_consecutive_safe < 1:
        raise ValueError("invalid host safety admission wait configuration")
    started = clock()
    safe_streak = 0
    samples: list[dict[str, Any]] = []
    while True:
        evidence = _with_host_io_hard_limit(collector())
        if block_io_sampler is not None:
            block_io = block_io_sampler.sample()
            evidence = dict(evidence)
            evidence["block_io"] = block_io
            checks = dict(evidence.get("checks") or {})
            block_reason = (
                "HOST_BLOCK_DEVICE_TELEMETRY_INCOMPLETE"
                if block_io.get("status") == "unavailable"
                else "HOST_BLOCK_DEVICE_NOT_QUIET"
            )
            checks["host_block_io"] = {
                "ok": block_io.get("safe") is True,
                "value": block_io,
                "reason_code": block_reason,
            }
            evidence["checks"] = checks
            if block_io.get("safe") is not True:
                evidence["tripped"] = list(dict.fromkeys([
                    *(evidence.get("tripped") or []),
                    block_reason,
                ]))
                evidence["ok"] = False
        tripped = [str(item) for item in evidence.get("tripped") or ()]
        samples.append({
            "at": evidence.get("at"),
            "ok": evidence.get("ok") is True,
            "tripped": tripped,
            "host_io_pressure": (
                (evidence.get("checks") or {})
                .get("host_io_pressure", {})
                .get("value")
            ),
        })
        if evidence.get("ok") is True:
            safe_streak += 1
            if safe_streak >= required_consecutive_safe:
                result = dict(evidence)
                result["admission_wait"] = {
                    "ok": True,
                    "waited_seconds": round(max(0.0, clock() - started), 6),
                    "sample_count": len(samples),
                    "required_consecutive_safe": required_consecutive_safe,
                    "samples": samples,
                }
                return result
        else:
            safe_streak = 0
            non_waitable = [
                reason
                for reason in tripped
                if reason not in WAITABLE_HOST_SAFETY_REASONS
            ]
            if non_waitable:
                result = dict(evidence)
                result["admission_wait"] = {
                    "ok": False,
                    "waited_seconds": round(max(0.0, clock() - started), 6),
                    "sample_count": len(samples),
                    "required_consecutive_safe": required_consecutive_safe,
                    "non_waitable": non_waitable,
                    "samples": samples,
                }
                return result
        elapsed = max(0.0, clock() - started)
        if elapsed >= timeout_seconds:
            result = dict(evidence)
            timeout_reason = "HOST_SAFETY_ADMISSION_TIMEOUT"
            result["tripped"] = list(dict.fromkeys([*tripped, timeout_reason]))
            result["ok"] = False
            result["admission_wait"] = {
                "ok": False,
                "waited_seconds": round(elapsed, 6),
                "sample_count": len(samples),
                "required_consecutive_safe": required_consecutive_safe,
                "timeout": True,
                "samples": samples,
            }
            return result
        sleeper(min(poll_seconds, max(0.0, timeout_seconds - elapsed)))


def _nested_get(payload: Mapping[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _valid_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(
        len(ordered) - 1,
        max(0, int((len(ordered) - 1) * float(fraction))),
    )
    return round(ordered[index], 3)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int
    boot_id: str
    cgroup_path: str
    state: str


def _unified_cgroup(proc_root: Path, pid: int) -> str:
    for row in _read_text(proc_root / str(int(pid)) / "cgroup").splitlines():
        parts = row.split(":", 2)
        if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
            return "/" + parts[2].strip().lstrip("/")
    raise RuntimeError(f"pid {pid} has no unified cgroup v2 membership")


def _process_stat_identity(pid: int, *, proc_root: Path) -> tuple[int, str]:
    tail = _read_text(proc_root / str(int(pid)) / "stat").rsplit(") ", 1)[1].split()
    start_ticks = int(tail[19])
    state = str(tail[0])
    if start_ticks <= 0 or len(state) != 1:
        raise RuntimeError(f"invalid process identity for pid {pid}")
    return start_ticks, state


def capture_process_identity(
    pid: int,
    *,
    proc_root: Path = Path("/proc"),
) -> ProcessIdentity:
    first_start, first_state = _process_stat_identity(pid, proc_root=proc_root)
    boot_id = _read_text(proc_root / "sys" / "kernel" / "random" / "boot_id")
    cgroup_path = _unified_cgroup(proc_root, pid)
    second_start, second_state = _process_stat_identity(pid, proc_root=proc_root)
    if first_start != second_start:
        raise RuntimeError(f"pid {pid} identity changed during attestation")
    if first_state == "Z" or second_state == "Z":
        raise RuntimeError(f"pid {pid} is a zombie")
    if not boot_id:
        raise RuntimeError("kernel boot identity is empty")
    return ProcessIdentity(int(pid), second_start, boot_id, cgroup_path, second_state)


def _within_cgroup(actual: str, expected: str) -> bool:
    actual_path = "/" + str(actual or "").strip().lstrip("/")
    expected_path = "/" + str(expected or "").strip().lstrip("/")
    return actual_path == expected_path or actual_path.startswith(
        expected_path.rstrip("/") + "/"
    )


class ProcessRoleRegistry:
    """Tracks roots whose complete descendant trees must remain observable."""

    def __init__(
        self,
        *,
        expected_cgroup: str = "",
        required_roles: Iterable[str] = (),
        proc_root: Path = Path("/proc"),
    ) -> None:
        self._lock = threading.RLock()
        self._roles: dict[str, dict[ProcessIdentity, bool]] = {}
        self._observed_roles: set[str] = set()
        self.expected_cgroup = str(expected_cgroup or "")
        self.required_roles = frozenset(str(role) for role in required_roles)
        self.proc_root = Path(proc_root)

    def register(self, role: str, pid: int, *, required: bool = True, start_ticks: int | None = None) -> ProcessIdentity:
        identity = capture_process_identity(int(pid), proc_root=self.proc_root)
        if start_ticks is not None and int(start_ticks) != identity.start_ticks:
            raise RuntimeError(
                f"pid {pid} starttime mismatch: expected={start_ticks}, actual={identity.start_ticks}"
            )
        if self.expected_cgroup and not _within_cgroup(
            identity.cgroup_path, self.expected_cgroup
        ):
            raise RuntimeError(
                f"pid_outside_campaign_cgroup:{pid}:{identity.cgroup_path}:"
                f"expected={self.expected_cgroup}"
            )
        with self._lock:
            self._roles.setdefault(str(role), {})[identity] = bool(required)
            self._observed_roles.add(str(role))
        return identity

    def unregister(self, role: str, identity: ProcessIdentity) -> None:
        with self._lock:
            rows = self._roles.get(str(role), {})
            rows.pop(identity, None)
            if not rows:
                self._roles.pop(str(role), None)

    def snapshot(self) -> dict[str, dict[ProcessIdentity, bool]]:
        with self._lock:
            return {role: dict(rows) for role, rows in self._roles.items()}

    def coverage(self) -> dict[str, Any]:
        with self._lock:
            required = set(self.required_roles)
            observed = set(self._observed_roles)
        missing = sorted(required - observed)
        return {
            "required_roles": sorted(required),
            "observed_roles": sorted(observed & required),
            "extra_roles": sorted(observed - required),
            "missing_roles": missing,
            "coverage_ratio": round(len(required & observed) / len(required), 6)
            if required
            else 1.0,
            "ok": not missing,
        }


def process_start_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int:
    return _process_stat_identity(pid, proc_root=proc_root)[0]


def process_table(*, proc_root: Path = Path("/proc")) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status: dict[str, str] = {}
            for line in _read_text(entry / "status").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    status[key] = value.strip()
            stat_tail = _read_text(entry / "stat").rsplit(") ", 1)[1].split()
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
            try:
                fd_count = sum(1 for _ in (entry / "fd").iterdir())
            except Exception:
                fd_count = -1
            rows[int(entry.name)] = {
                "ppid": int(status.get("PPid", "0").split()[0]),
                "pgrp": int(stat_tail[2]),
                "session": int(stat_tail[3]),
                "rss_bytes": int(status.get("VmRSS", "0 kB").split()[0]) * 1024,
                "threads": int(status.get("Threads", "0").split()[0]),
                "cpu_ticks": int(stat_tail[11]) + int(stat_tail[12]),
                "start_ticks": int(stat_tail[19]),
                "cgroup_path": _unified_cgroup(proc_root, int(entry.name)),
                "fd_count": fd_count,
                "cmdline": cmdline,
            }
        except Exception:
            continue
    return rows


def descendants(rows: Mapping[int, Mapping[str, Any]], roots: Iterable[int]) -> set[int]:
    found = {int(pid) for pid in roots if int(pid) in rows}
    changed = True
    while changed:
        changed = False
        for pid, row in rows.items():
            if pid not in found and int(row.get("ppid") or 0) in found:
                found.add(pid)
                changed = True
    return found


@dataclass
class ResourceCollectorConfig:
    cgroup_path: Path
    sample_path: Path
    runtime_roots: Mapping[str, Path]
    campaign_data_root: Path
    process_registry: ProcessRoleRegistry = field(default_factory=ProcessRoleRegistry)
    proc_root: Path = Path("/proc")
    require_gpu: bool = False
    gpu_command: tuple[str, ...] = (
        "nvidia-smi",
        "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    )
    comfyui_queue_url: str = ""
    require_comfyui_queue: bool = False
    # Leave enough headroom for WSL, the database, the watchdog, and the
    # desktop host.  The campaign must stop before global reclaim or swap can
    # make the machine unresponsive; a cgroup OOM limit alone is too late.
    minimum_host_mem_available_bytes: int = DEFAULT_MINIMUM_HOST_MEM_AVAILABLE_BYTES
    minimum_disk_free_bytes: int = 20 * GIB
    maximum_host_load1_per_cpu: float = DEFAULT_MAXIMUM_HOST_LOAD1_PER_CPU
    maximum_host_cpu_pressure_some_avg10: float = DEFAULT_MAXIMUM_HOST_CPU_PRESSURE_SOME_AVG10
    maximum_host_memory_pressure_full_avg10: float = DEFAULT_MAXIMUM_HOST_MEMORY_PRESSURE_FULL_AVG10
    maximum_host_io_pressure_full_avg10: float = DEFAULT_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG10
    maximum_host_io_pressure_full_avg60: float = DEFAULT_MAXIMUM_HOST_IO_PRESSURE_FULL_AVG60
    maximum_host_swap_used_bytes: int = DEFAULT_MAXIMUM_HOST_SWAP_USED_BYTES
    maximum_gpu_vram_ratio: float = 0.85
    maximum_gpu_temperature_c: float = 80.0
    expected_cgroup_limits: Mapping[str, str] = field(default_factory=lambda: {
        "memory.high": str(5 * GIB),
        "memory.max": str(6 * GIB),
        "memory.swap.max": str(512 * MIB),
        "cpu.max": "300000 100000",
        "pids.max": "384",
    })
    expected_process_cgroup: str = ""
    cgroup_event_baseline: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    health_targets: Mapping[str, str] = field(default_factory=dict)
    health_timeout_seconds: float = 5.0


class ResourceCollector:
    """Collects one complete, self-describing campaign resource sample."""

    BASE_EXPECTED_FIELDS = frozenset({
        "host.load.load1",
        "host.load.load5",
        "host.load.load15",
        "host.memory.available_bytes",
        "host.memory.total_bytes",
        "host.swap.used_bytes",
        "host.swap.total_bytes",
        "host.disk.free_bytes",
        "host.disk.total_bytes",
        "host.block_io.read_bytes_per_second",
        "host.block_io.write_bytes_per_second",
        "host.block_io.await_ms",
        "host.block_io.util_percent",
        "host.psi.cpu.some.avg10",
        "host.psi.memory.some.avg10",
        "host.psi.memory.full.avg10",
        "host.psi.io.some.avg10",
        "host.psi.io.full.avg10",
        "host.psi.io.full.avg60",
        "cgroup.cpu.usage_usec",
        "cgroup.cpu.nr_throttled",
        "cgroup.memory.current_bytes",
        "cgroup.memory.swap_current_bytes",
        "cgroup.memory.events.oom",
        "cgroup.memory.events.oom_kill",
        "cgroup.pids.current",
        "cgroup.limits_verified",
        "databases.total_bytes",
        "databases.wal_bytes",
        "databases.wal_growth_bytes_per_second",
        "hard_limit_state.ok",
    })

    def __init__(self, config: ResourceCollectorConfig):
        self.config = config
        self._previous_disk: dict[str, Any] | None = None
        self._previous_wal: tuple[int, int] | None = None
        memory_baseline = config.cgroup_event_baseline.get("memory.events") or {}
        self._baseline_oom_kill: int | None = (
            int(memory_baseline["oom_kill"])
            if "oom_kill" in memory_baseline
            else None
        )
        self._tracked_descendants: dict[str, set[ProcessIdentity]] = {}
        self.samples: list[dict[str, Any]] = []

    def _collect_host(self, errors: dict[str, str], *, monotonic_ns: int) -> dict[str, Any]:
        host: dict[str, Any] = {}
        try:
            load = _read_text(self.config.proc_root / "loadavg").split()
            host["load"] = {"load1": float(load[0]), "load5": float(load[1]), "load15": float(load[2])}
        except Exception as exc:
            errors["host.load"] = f"{exc.__class__.__name__}: {exc}"
            host["load"] = {}
        try:
            mem = _meminfo(self.config.proc_root / "meminfo")
            total = int(mem.get("MemTotal", 0))
            available = int(mem.get("MemAvailable", 0))
            swap_total = int(mem.get("SwapTotal", 0))
            swap_free = int(mem.get("SwapFree", 0))
            host["memory"] = {"available_bytes": available, "total_bytes": total}
            host["swap"] = {"used_bytes": max(0, swap_total - swap_free), "total_bytes": swap_total}
        except Exception as exc:
            errors["host.memory"] = f"{exc.__class__.__name__}: {exc}"
            host.setdefault("memory", {})
            host.setdefault("swap", {})
        try:
            stats = os.statvfs(self.config.campaign_data_root)
            host["disk"] = {
                "free_bytes": int(stats.f_bavail * stats.f_frsize),
                "total_bytes": int(stats.f_blocks * stats.f_frsize),
            }
        except Exception as exc:
            errors["host.disk"] = f"{exc.__class__.__name__}: {exc}"
            host["disk"] = {}
        for name in ("cpu", "memory", "io"):
            try:
                host.setdefault("psi", {})[name] = _parse_psi(self.config.proc_root / "pressure" / name)
            except Exception as exc:
                errors[f"host.psi.{name}"] = f"{exc.__class__.__name__}: {exc}"
                host.setdefault("psi", {})[name] = {}
        host["block_io"] = self._collect_block_io(errors, monotonic_ns=monotonic_ns)
        return host

    def _collect_block_io(self, errors: dict[str, str], *, monotonic_ns: int) -> dict[str, Any]:
        try:
            device = os.stat(self.config.campaign_data_root).st_dev
            wanted = (os.major(device), os.minor(device))
            selected: list[str] | None = None
            for line in _read_text(self.config.proc_root / "diskstats").splitlines():
                parts = line.split()
                if len(parts) >= 14 and (int(parts[0]), int(parts[1])) == wanted:
                    selected = parts
                    break
            if not selected:
                raise RuntimeError(f"diskstats device not found: {wanted[0]}:{wanted[1]}")
            current = {
                "monotonic_ns": monotonic_ns,
                "device": selected[2],
                "reads": int(selected[3]),
                "sectors_read": int(selected[5]),
                "read_ms": int(selected[6]),
                "writes": int(selected[7]),
                "sectors_written": int(selected[9]),
                "write_ms": int(selected[10]),
                "io_ms": int(selected[12]),
            }
            previous = self._previous_disk
            self._previous_disk = current
            if not previous or previous.get("device") != current["device"]:
                return {"device": current["device"], "read_bytes_per_second": None, "write_bytes_per_second": None, "await_ms": None, "util_percent": None}
            elapsed_seconds = max(0.000001, (monotonic_ns - int(previous["monotonic_ns"])) / 1_000_000_000)
            reads = max(0, current["reads"] - int(previous["reads"]))
            writes = max(0, current["writes"] - int(previous["writes"]))
            operations = reads + writes
            elapsed_io_ms = max(0, current["read_ms"] - int(previous["read_ms"])) + max(0, current["write_ms"] - int(previous["write_ms"]))
            return {
                "device": current["device"],
                "read_bytes_per_second": round(max(0, current["sectors_read"] - int(previous["sectors_read"])) * 512 / elapsed_seconds, 3),
                "write_bytes_per_second": round(max(0, current["sectors_written"] - int(previous["sectors_written"])) * 512 / elapsed_seconds, 3),
                "await_ms": round(elapsed_io_ms / operations, 3) if operations else 0.0,
                "util_percent": round(min(100.0, max(0, current["io_ms"] - int(previous["io_ms"])) / (elapsed_seconds * 10)), 3),
            }
        except Exception as exc:
            errors["host.block_io"] = f"{exc.__class__.__name__}: {exc}"
            return {}

    def _collect_cgroup(self, errors: dict[str, str]) -> dict[str, Any]:
        base = self.config.cgroup_path
        result: dict[str, Any] = {"path": str(base)}
        try:
            cpu = _key_value_lines(_read_text(base / "cpu.stat"))
            result["cpu"] = {
                "usage_usec": cpu.get("usage_usec"),
                "nr_throttled": cpu.get("nr_throttled"),
                "throttled_usec": cpu.get("throttled_usec"),
            }
        except Exception as exc:
            errors["cgroup.cpu"] = f"{exc.__class__.__name__}: {exc}"
            result["cpu"] = {}
        try:
            events = _key_value_lines(_read_text(base / "memory.events"))
            swap_events = (
                _key_value_lines(_read_text(base / "memory.swap.events"))
                if (base / "memory.swap.events").exists()
                else {}
            )
            result["memory"] = {
                "current_bytes": _read_int(base / "memory.current"),
                "swap_current_bytes": _read_int(base / "memory.swap.current"),
                "events": events,
                "swap_events": swap_events,
            }
        except Exception as exc:
            errors["cgroup.memory"] = f"{exc.__class__.__name__}: {exc}"
            result["memory"] = {"events": {}}
        try:
            result["pids"] = {
                "current": _read_int(base / "pids.current"),
                "events": _key_value_lines(_read_text(base / "pids.events")),
            }
        except Exception as exc:
            errors["cgroup.pids"] = f"{exc.__class__.__name__}: {exc}"
            result["pids"] = {}
        actual: dict[str, str] = {}
        for name in self.config.expected_cgroup_limits:
            try:
                actual[name] = _read_text(base / name)
            except Exception as exc:
                errors[f"cgroup.limit.{name}"] = f"{exc.__class__.__name__}: {exc}"
        mismatches = {
            name: {"expected": expected, "actual": actual.get(name)}
            for name, expected in self.config.expected_cgroup_limits.items()
            if actual.get(name) != str(expected)
        }
        result["limits"] = actual
        result["limit_mismatches"] = mismatches
        result["limits_verified"] = not mismatches and len(actual) == len(self.config.expected_cgroup_limits)
        return result

    def _collect_databases(self, errors: dict[str, str], *, monotonic_ns: int) -> dict[str, Any]:
        total = 0
        wal = 0
        files: dict[str, int] = {}
        try:
            for label, runtime in self.config.runtime_roots.items():
                database_dir = Path(runtime) / "database"
                if not database_dir.exists():
                    continue
                for path in database_dir.glob("*.db*"):
                    if not path.is_file():
                        continue
                    size = path.stat().st_size
                    files[f"{label}/{path.name}"] = size
                    total += size
                    if path.name.endswith("-wal"):
                        wal += size
        except Exception as exc:
            errors["databases"] = f"{exc.__class__.__name__}: {exc}"
        growth: float | None = None
        if self._previous_wal is not None:
            previous_ns, previous_bytes = self._previous_wal
            elapsed = max(0.000001, (monotonic_ns - previous_ns) / 1_000_000_000)
            growth = round((wal - previous_bytes) / elapsed, 3)
        self._previous_wal = (monotonic_ns, wal)
        return {"total_bytes": total, "wal_bytes": wal, "wal_growth_bytes_per_second": growth, "files": files}

    def _collect_process_roles(self, errors: dict[str, str]) -> tuple[dict[str, Any], set[str]]:
        role_rows: dict[str, Any] = {}
        expected: set[str] = set()
        snapshots = self.config.process_registry.snapshot()
        roles = (
            set(snapshots)
            | set(self._tracked_descendants)
            | set(self.config.process_registry.required_roles)
        )
        if not roles:
            return role_rows, expected
        try:
            table = process_table(proc_root=self.config.proc_root)
        except Exception as exc:
            errors["processes"] = f"{exc.__class__.__name__}: {exc}"
            return role_rows, expected
        try:
            boot_id = _read_text(
                self.config.proc_root / "sys" / "kernel" / "random" / "boot_id"
            )
        except Exception as exc:
            errors["processes.boot_id"] = f"{exc.__class__.__name__}: {exc}"
            boot_id = ""
        expected_cgroup = str(
            self.config.expected_process_cgroup
            or self.config.process_registry.expected_cgroup
            or ""
        )
        for role in sorted(roles):
            identities = snapshots.get(role, {})
            roots: list[int] = []
            identity_errors: list[str] = []
            required = any(identities.values()) or role in self.config.process_registry.required_roles
            for identity in identities:
                row = table.get(identity.pid)
                if not row or int(row.get("start_ticks") or -1) != identity.start_ticks:
                    identity_errors.append(f"pid_identity_missing:{identity.pid}:{identity.start_ticks}")
                else:
                    if boot_id and identity.boot_id != boot_id:
                        identity_errors.append(
                            f"pid_boot_identity_changed:{identity.pid}:{identity.boot_id}:{boot_id}"
                        )
                    membership = str(row.get("cgroup_path") or "")
                    if membership != identity.cgroup_path:
                        identity_errors.append(
                            f"pid_cgroup_changed:{identity.pid}:{identity.cgroup_path}:{membership}"
                        )
                    if expected_cgroup and not _within_cgroup(membership, expected_cgroup):
                        identity_errors.append(
                            f"pid_outside_campaign_cgroup:{identity.pid}:{membership}"
                        )
                    roots.append(identity.pid)
            tree = descendants(table, roots)
            tracked = self._tracked_descendants.setdefault(role, set())
            still_live: set[ProcessIdentity] = set()
            for identity in tracked:
                row = table.get(identity.pid)
                if row and int(row.get("start_ticks") or -1) == identity.start_ticks:
                    still_live.add(identity)
                    membership = str(row.get("cgroup_path") or "")
                    if expected_cgroup and not _within_cgroup(membership, expected_cgroup):
                        identity_errors.append(
                            f"tracked_descendant_outside_campaign_cgroup:{identity.pid}:{membership}"
                        )
            for pid in sorted(tree):
                row = table[pid]
                membership = str(row.get("cgroup_path") or "")
                if expected_cgroup and not _within_cgroup(membership, expected_cgroup):
                    identity_errors.append(
                        f"descendant_outside_campaign_cgroup:{pid}:{membership}"
                    )
                if boot_id:
                    still_live.add(ProcessIdentity(
                        int(pid),
                        int(row.get("start_ticks") or 0),
                        boot_id,
                        membership,
                        "?",
                    ))
            self._tracked_descendants[role] = still_live
            role_rows[role] = {
                "root_pids": sorted(roots),
                "pids": sorted(tree),
                "process_count": len(tree),
                "rss_bytes": sum(int(table[pid].get("rss_bytes") or 0) for pid in tree),
                "threads": sum(int(table[pid].get("threads") or 0) for pid in tree),
                "fd_count": sum(max(0, int(table[pid].get("fd_count") or 0)) for pid in tree),
                "cpu_ticks": sum(int(table[pid].get("cpu_ticks") or 0) for pid in tree),
                "identity_errors": identity_errors,
                "containment_ok": not identity_errors,
                "attested_cgroup": expected_cgroup,
                "cmdline_samples": [str(table[pid].get("cmdline") or "")[:300] for pid in sorted(tree)[:10]],
            }
            if required:
                for name in (
                    "process_count",
                    "rss_bytes",
                    "threads",
                    "fd_count",
                    "cpu_ticks",
                    "containment_ok",
                ):
                    expected.add(f"process_roles.{role}.{name}")
                if identity_errors:
                    errors[f"process_roles.{role}"] = ",".join(identity_errors)
        return role_rows, expected

    def _collect_gpu(self, errors: dict[str, str]) -> list[dict[str, Any]]:
        try:
            completed = subprocess.run(
                list(self.config.gpu_command),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError((completed.stderr or completed.stdout or f"returncode={completed.returncode}")[:500])
            rows: list[dict[str, Any]] = []
            for line in completed.stdout.splitlines():
                parts = [part.strip() for part in line.split(",")]
                if len(parts) < 5:
                    continue
                rows.append({
                    "index": int(parts[0]),
                    "utilization_percent": float(parts[1]),
                    "memory_used_mib": float(parts[2]),
                    "memory_total_mib": float(parts[3]),
                    "temperature_c": float(parts[4]),
                })
            if not rows:
                raise RuntimeError("no GPU rows returned")
            return rows
        except Exception as exc:
            errors["gpu"] = f"{exc.__class__.__name__}: {exc}"
            return []

    def _collect_comfyui_queue(self, errors: dict[str, str]) -> dict[str, Any]:
        if not self.config.comfyui_queue_url:
            return {}
        try:
            response = requests.get(self.config.comfyui_queue_url, timeout=5)
            payload = response.json()
            if response.status_code != 200 or not isinstance(payload, dict):
                raise RuntimeError(f"status={response.status_code}")
            running = payload.get("queue_running") or []
            pending = payload.get("queue_pending") or []
            return {"running": len(running), "pending": len(pending), "status": response.status_code}
        except Exception as exc:
            errors["comfyui_queue"] = f"{exc.__class__.__name__}: {exc}"
            return {}

    def _collect_health(self, errors: dict[str, str]) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for label, base_url in sorted(self.config.health_targets.items()):
            started = time.perf_counter()
            status_code = 0
            semantic_ready = False
            error = ""
            try:
                response = requests.get(
                    f"{str(base_url).rstrip('/')}/api/readyz",
                    headers={"Connection": "close"},
                    timeout=max(0.1, float(self.config.health_timeout_seconds)),
                    verify=False,
                )
                status_code = int(response.status_code)
                payload = response.json()
                checks = payload.get("checks") if isinstance(payload, dict) else None
                database = checks.get("db") if isinstance(checks, dict) else None
                semantic_ready = bool(
                    status_code == 200
                    and isinstance(payload, dict)
                    and payload.get("ok") is True
                    and payload.get("status") == "ready"
                    and isinstance(database, dict)
                    and database.get("ok") is True
                    and isinstance(payload.get("backpressure"), dict)
                )
                if not semantic_ready:
                    error = "readiness_semantic_invariant_failed"
            except Exception as exc:
                error = f"{exc.__class__.__name__}: {exc}"
                errors[f"health.{label}"] = error
            results[str(label)] = {
                "status_code": status_code,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "semantic_ready": semantic_ready,
                "error": error,
            }
        return results

    def _hard_limits(self, sample: Mapping[str, Any]) -> dict[str, Any]:
        oom_kill = _nested_get(sample, "cgroup.memory.events.oom_kill")
        if (
            not self.config.cgroup_event_baseline
            and isinstance(oom_kill, int)
            and self._baseline_oom_kill is None
        ):
            self._baseline_oom_kill = oom_kill
        counter_checks: dict[str, dict[str, Any]] = {}
        counter_paths = {
            "memory.events": "cgroup.memory.events",
            "memory.swap.events": "cgroup.memory.swap_events",
            "pids.events": "cgroup.pids.events",
        }
        for filename, baseline_values in self.config.cgroup_event_baseline.items():
            prefix = counter_paths.get(filename)
            if not prefix:
                continue
            for name, baseline in baseline_values.items():
                value = _nested_get(sample, f"{prefix}.{name}")
                delta = (
                    int(value) - int(baseline)
                    if isinstance(value, int) and not isinstance(value, bool)
                    else None
                )
                counter_checks[f"{filename}.{name}"] = {
                    "ok": delta == 0,
                    "value": value,
                    "baseline": int(baseline),
                    "delta": delta,
                }
        event_counter_ok = bool(counter_checks) and all(
            row["ok"] for row in counter_checks.values()
        ) if self.config.cgroup_event_baseline else (
            isinstance(oom_kill, int)
            and self._baseline_oom_kill is not None
            and oom_kill <= self._baseline_oom_kill
        )
        process_role_rows = (
            sample.get("process_roles")
            if isinstance(sample.get("process_roles"), Mapping)
            else {}
        )
        containment_errors = {
            str(role): list(row.get("identity_errors") or [])
            for role, row in process_role_rows.items()
            if isinstance(row, Mapping) and row.get("identity_errors")
        }
        host = sample.get("host") if isinstance(sample.get("host"), Mapping) else {}
        checks = _host_safety_checks(
            host,
            minimum_host_mem_available_bytes=self.config.minimum_host_mem_available_bytes,
            maximum_host_load1_per_cpu=self.config.maximum_host_load1_per_cpu,
            maximum_host_cpu_pressure_some_avg10=self.config.maximum_host_cpu_pressure_some_avg10,
            maximum_host_memory_pressure_full_avg10=self.config.maximum_host_memory_pressure_full_avg10,
            maximum_host_io_pressure_full_avg10=self.config.maximum_host_io_pressure_full_avg10,
            maximum_host_io_pressure_full_avg60=self.config.maximum_host_io_pressure_full_avg60,
            maximum_host_swap_used_bytes=self.config.maximum_host_swap_used_bytes,
        )
        checks.update({
            "disk_free": {
                "ok": int(_nested_get(sample, "host.disk.free_bytes") or 0) >= self.config.minimum_disk_free_bytes,
                "value": _nested_get(sample, "host.disk.free_bytes"),
                "minimum": self.config.minimum_disk_free_bytes,
                "reason_code": "CAMPAIGN_DISK_FREE_LOW",
            },
            "cgroup_oom": {
                "ok": event_counter_ok,
                "value": counter_checks or oom_kill,
                "baseline": self.config.cgroup_event_baseline or self._baseline_oom_kill,
                "reason_code": "CGROUP_OOM_COUNTER_INCREASED",
            },
            "cgroup_limits": {
                "ok": _nested_get(sample, "cgroup.limits_verified") is True,
                "value": _nested_get(sample, "cgroup.limit_mismatches"),
                "reason_code": "CGROUP_LIMIT_DRIFT",
            },
            "process_containment": {
                "ok": not containment_errors,
                "value": containment_errors,
                "reason_code": "PROCESS_CONTAINMENT_VIOLATION",
            },
        })
        health = sample.get("health") if isinstance(sample.get("health"), Mapping) else {}
        for label in sorted(self.config.health_targets):
            row = health.get(label) if isinstance(health, Mapping) else None
            checks[f"health_{label}"] = {
                "ok": isinstance(row, Mapping) and row.get("semantic_ready") is True,
                "value": dict(row) if isinstance(row, Mapping) else None,
                "reason_code": f"{str(label).upper()}_READINESS_LOST",
            }
        if self.config.require_gpu:
            gpu_rows = sample.get("gpu") if isinstance(sample.get("gpu"), list) else []
            ratios = [float(row.get("memory_used_mib") or 0) / max(1.0, float(row.get("memory_total_mib") or 0)) for row in gpu_rows]
            temperatures = [float(row.get("temperature_c") or 0) for row in gpu_rows]
            checks["gpu_vram"] = {
                "ok": bool(ratios) and max(ratios) <= self.config.maximum_gpu_vram_ratio,
                "value": max(ratios) if ratios else None,
                "maximum": self.config.maximum_gpu_vram_ratio,
                "reason_code": "GPU_VRAM_PRESSURE",
            }
            checks["gpu_temperature"] = {
                "ok": bool(temperatures) and max(temperatures) <= self.config.maximum_gpu_temperature_c,
                "value": max(temperatures) if temperatures else None,
                "maximum": self.config.maximum_gpu_temperature_c,
                "reason_code": "GPU_TEMPERATURE_HIGH",
            }
        tripped = [row["reason_code"] for row in checks.values() if not row["ok"]]
        return {"ok": not tripped, "tripped": tripped, "checks": checks}

    def collect(self, *, monotonic_ns: int | None = None) -> dict[str, Any]:
        tick_ns = int(monotonic_ns if monotonic_ns is not None else time.monotonic_ns())
        errors: dict[str, str] = {}
        sample: dict[str, Any] = {
            "sample_schema_version": RESOURCE_SAMPLE_SCHEMA_VERSION,
            "at": utc_now(),
            "monotonic_ns": tick_ns,
        }
        sample["host"] = self._collect_host(errors, monotonic_ns=tick_ns)
        sample["cgroup"] = self._collect_cgroup(errors)
        sample["databases"] = self._collect_databases(errors, monotonic_ns=tick_ns)
        process_roles, process_expected = self._collect_process_roles(errors)
        sample["process_roles"] = process_roles
        sample["gpu"] = self._collect_gpu(errors) if self.config.require_gpu else []
        sample["comfyui_queue"] = self._collect_comfyui_queue(errors)
        sample["health"] = self._collect_health(errors)
        sample["hard_limit_state"] = self._hard_limits(sample)

        expected = set(self.BASE_EXPECTED_FIELDS) | process_expected
        if self.config.require_gpu:
            expected |= {
                "gpu.0.utilization_percent",
                "gpu.0.memory_used_mib",
                "gpu.0.memory_total_mib",
                "gpu.0.temperature_c",
            }
        if self.config.require_comfyui_queue:
            expected |= {"comfyui_queue.running", "comfyui_queue.pending", "comfyui_queue.status"}
        for label in self.config.health_targets:
            expected |= {
                f"health.{label}.status_code",
                f"health.{label}.latency_ms",
                f"health.{label}.semantic_ready",
            }

        # Array field paths are normalized for the first mandatory local GPU.
        normalized: dict[str, Any] = dict(sample)
        if sample["gpu"]:
            normalized["gpu"] = {str(index): row for index, row in enumerate(sample["gpu"])}
        valid = sorted(path for path in expected if _valid_value(_nested_get(normalized, path)))
        missing = sorted(expected - set(valid))
        sample.update({
            "expected_fields": sorted(expected),
            "valid_fields": valid,
            "missing_fields": missing,
            "collector_errors": errors,
            "field_completeness_ratio": round(len(valid) / len(expected), 6) if expected else 0.0,
        })
        self.samples.append(sample)
        self.config.sample_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.sample_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
        return sample

    def summary(self, *, minimum_ratio: float = 0.95) -> dict[str, Any]:
        field_counts: dict[str, dict[str, int]] = {}
        total_expected = 0
        total_valid = 0
        hard_stops: list[dict[str, Any]] = []
        containment_violations: list[dict[str, Any]] = []
        for sample in self.samples:
            expected = set(sample.get("expected_fields") or [])
            valid = set(sample.get("valid_fields") or [])
            total_expected += len(expected)
            total_valid += len(valid)
            for name in expected:
                row = field_counts.setdefault(name, {"expected": 0, "valid": 0})
                row["expected"] += 1
                row["valid"] += int(name in valid)
            if not (sample.get("hard_limit_state") or {}).get("ok"):
                hard_stops.append({"at": sample.get("at"), "tripped": (sample.get("hard_limit_state") or {}).get("tripped")})
            role_errors = {
                str(role): list(row.get("identity_errors") or [])
                for role, row in (sample.get("process_roles") or {}).items()
                if isinstance(row, Mapping) and row.get("identity_errors")
            }
            if role_errors:
                containment_violations.append({
                    "at": sample.get("at"),
                    "roles": role_errors,
                })
        fields = {
            name: {
                **row,
                "validity_ratio": round(row["valid"] / row["expected"], 6) if row["expected"] else 0.0,
            }
            for name, row in sorted(field_counts.items())
        }
        below = sorted(name for name, row in fields.items() if float(row["validity_ratio"]) < minimum_ratio)
        overall = round(total_valid / total_expected, 6) if total_expected else 0.0
        servers: dict[str, Any] = {}
        for label in sorted(self.config.health_targets):
            health_rows = [
                (sample.get("health") or {}).get(label) or {}
                for sample in self.samples
            ]
            ready_rows = [row for row in health_rows if row.get("semantic_ready") is True]
            failed_rows = [row for row in health_rows if row.get("semantic_ready") is not True]
            latencies = [float(row.get("latency_ms") or 0.0) for row in ready_rows]
            process_rows = [
                (sample.get("process_roles") or {}).get(label) or {}
                for sample in self.samples
            ]
            servers[label] = {
                "samples": len(health_rows),
                "health_200": sum(1 for row in health_rows if int(row.get("status_code") or 0) == 200),
                "semantic_ready_samples": len(ready_rows),
                "unplanned_health_failures": len(failed_rows),
                "unplanned_failure_samples": failed_rows[:20],
                "health_latency_ms": {
                    "p50": _percentile(latencies, 0.50),
                    "p95": _percentile(latencies, 0.95),
                    "p99": _percentile(latencies, 0.99),
                    "max": round(max(latencies), 3) if latencies else 0.0,
                },
                "max_rss_mb": round(
                    max((int(row.get("rss_bytes") or 0) for row in process_rows), default=0)
                    / 1024**2,
                    3,
                ),
                "max_threads": max(
                    (int(row.get("threads") or 0) for row in process_rows),
                    default=0,
                ),
                "max_fd_count": max(
                    (int(row.get("fd_count") or 0) for row in process_rows),
                    default=0,
                ),
            }
        role_coverage = self.config.process_registry.coverage()
        return {
            "sample_schema_version": RESOURCE_SAMPLE_SCHEMA_VERSION,
            "samples": len(self.samples),
            "expected_values": total_expected,
            "valid_values": total_valid,
            "mandatory_field_completeness": overall,
            "minimum_required_ratio": float(minimum_ratio),
            "fields": fields,
            "mandatory_fields_below_threshold": below,
            "hard_stop_samples": hard_stops,
            "process_containment_violation_count": len(containment_violations),
            "process_containment_violations": containment_violations[:100],
            "actual_role_coverage": role_coverage,
            "servers": servers,
            "ok": bool(
                self.samples
                and overall >= minimum_ratio
                and not below
                and not hard_stops
                and not containment_violations
                and role_coverage.get("ok") is True
            ),
        }


class ResourceMonitor(threading.Thread):
    """Periodic collector that invokes hard-stop synchronously on first trip."""

    def __init__(
        self,
        collector: ResourceCollector,
        *,
        interval_seconds: float,
        hard_stop: Callable[[dict[str, Any]], None],
    ) -> None:
        super().__init__(daemon=True, name="campaign-resource-monitor")
        self.collector = collector
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.hard_stop = hard_stop
        self.stop_event = threading.Event()
        self.failure: str | None = None

    @property
    def out(self) -> Path:
        return self.collector.config.sample_path

    @property
    def samples(self) -> list[dict[str, Any]]:
        return self.collector.samples

    def summary(self) -> dict[str, Any]:
        result = self.collector.summary(minimum_ratio=0.95)
        if self.failure:
            result["monitor_failure"] = self.failure
            result["ok"] = False
        return result

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                sample = self.collector.collect()
                hard_limit_state = sample.get("hard_limit_state") or {}
                if not hard_limit_state.get("ok"):
                    self.hard_stop(sample)
                    self.stop_event.set()
                    return
            except Exception as exc:
                self.failure = f"{exc.__class__.__name__}: {exc}"
                self.hard_stop({
                    "sample_schema_version": RESOURCE_SAMPLE_SCHEMA_VERSION,
                    "at": utc_now(),
                    "hard_limit_state": {
                        "ok": False,
                        "tripped": ["RESOURCE_COLLECTOR_FAILED"],
                        "error": self.failure,
                    },
                })
                self.stop_event.set()
                return
            self.stop_event.wait(self.interval_seconds)
