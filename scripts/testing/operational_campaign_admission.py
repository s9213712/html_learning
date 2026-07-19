#!/usr/bin/env python3
"""Wait without campaign resources, then exec the supervised campaign safely."""

from __future__ import annotations

import sys


_BOOTSTRAP_STARTED_WITHOUT_BYTECODE_WRITES = bool(sys.dont_write_bytecode)
sys.dont_write_bytecode = True


_BOOTSTRAP_SAMPLE_SECONDS = 5.0
_BOOTSTRAP_REQUIRED_SAFE_WINDOWS = 12
_BOOTSTRAP_MAX_IO_PSI_AVG10 = 3.0
_BOOTSTRAP_MAX_IO_PSI_AVG60 = 3.0
_BOOTSTRAP_MIN_MEM_AVAILABLE_KIB = 3 * 1024 * 1024
_BOOTSTRAP_MAX_SWAP_USED_KIB = 512 * 1024
_BOOTSTRAP_MAX_READ_AWAIT_MS = 60.0
_BOOTSTRAP_MAX_WRITE_AWAIT_MS = 60.0
_BOOTSTRAP_MAX_FLUSH_AWAIT_MS = 100.0
_BOOTSTRAP_MAX_AVERAGE_QUEUE_DEPTH = 0.25


def _bootstrap_float_option(name: str, default: float) -> float | None:
    """Parse one safety option without importing argparse."""

    prefix = name + "="
    arguments = sys.argv[1:]
    raw_value = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == name:
            if raw_value is not None or index + 1 >= len(arguments):
                return None
            raw_value = arguments[index + 1]
            index += 2
            continue
        if argument.startswith(prefix):
            if raw_value is not None:
                return None
            raw_value = argument[len(prefix):]
        index += 1
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if not value > 0.0 or value != value or value > 86400.0:
        return None
    return value


def _bootstrap_io_pressure() -> tuple[float, float] | None:
    try:
        with open("/proc/pressure/io", "rb") as handle:
            for line in handle:
                if not line.startswith(b"full "):
                    continue
                values: dict[bytes, float] = {}
                for token in line.split()[1:]:
                    key, separator, raw_value = token.partition(b"=")
                    if separator and key in {b"avg10", b"avg60"}:
                        values[key] = float(raw_value)
                if b"avg10" in values and b"avg60" in values:
                    return values[b"avg10"], values[b"avg60"]
    except (OSError, TypeError, ValueError):
        return None
    return None


def _bootstrap_memory() -> tuple[int, int] | None:
    wanted = {b"MemAvailable", b"SwapTotal", b"SwapFree"}
    values: dict[bytes, int] = {}
    try:
        with open("/proc/meminfo", "rb") as handle:
            for line in handle:
                name, separator, remainder = line.partition(b":")
                if separator and name in wanted:
                    fields = remainder.split()
                    if not fields:
                        return None
                    values[name] = int(fields[0])
    except (OSError, TypeError, ValueError):
        return None
    if wanted.difference(values):
        return None
    swap_used_kib = max(0, values[b"SwapTotal"] - values[b"SwapFree"])
    return values[b"MemAvailable"], swap_used_kib


def _bootstrap_root_device() -> tuple[int, int] | None:
    try:
        with open("/proc/self/mountinfo", "rb") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 6 or fields[4] != b"/":
                    continue
                major, separator, minor = fields[2].partition(b":")
                if not separator:
                    return None
                return int(major), int(minor)
    except (OSError, TypeError, ValueError):
        return None
    return None


def _bootstrap_disk_snapshot(
    clock: object,
    device: tuple[int, int],
) -> tuple[float, int, int, int, int, int, int, int, int] | None:
    """Read whole-device write, queue, and flush counters from diskstats."""

    try:
        with open("/proc/diskstats", "rb") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 20:
                    continue
                if int(fields[0]) != device[0] or int(fields[1]) != device[1]:
                    continue
                return (
                    clock.monotonic(),
                    int(fields[3]),
                    int(fields[6]),
                    int(fields[7]),
                    int(fields[10]),
                    int(fields[11]),
                    int(fields[13]),
                    int(fields[18]),
                    int(fields[19]),
                )
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return None


def _bootstrap_block_window_is_quiet(
    previous: tuple[float, int, int, int, int, int, int, int, int],
    current: tuple[float, int, int, int, int, int, int, int, int],
) -> bool | None:
    elapsed_seconds = current[0] - previous[0]
    if not 4.0 <= elapsed_seconds <= 7.5:
        return False
    monotonic_indexes = (1, 2, 3, 4, 6, 7, 8)
    if any(current[index] < previous[index] for index in monotonic_indexes):
        return None
    completed_reads = current[1] - previous[1]
    read_milliseconds = current[2] - previous[2]
    completed_writes = current[3] - previous[3]
    write_milliseconds = current[4] - previous[4]
    weighted_milliseconds = current[6] - previous[6]
    completed_flushes = current[7] - previous[7]
    flush_milliseconds = current[8] - previous[8]
    if (
        (completed_reads == 0 and read_milliseconds != 0)
        or (completed_writes == 0 and write_milliseconds != 0)
        or (completed_flushes == 0 and flush_milliseconds != 0)
    ):
        return None
    read_await_ms = (
        read_milliseconds / completed_reads if completed_reads else 0.0
    )
    write_await_ms = (
        write_milliseconds / completed_writes if completed_writes else 0.0
    )
    flush_await_ms = (
        flush_milliseconds / completed_flushes if completed_flushes else 0.0
    )
    average_queue_depth = weighted_milliseconds / (elapsed_seconds * 1000.0)
    return (
        previous[5] == 0
        and current[5] == 0
        and read_await_ms <= _BOOTSTRAP_MAX_READ_AWAIT_MS
        and write_await_ms <= _BOOTSTRAP_MAX_WRITE_AWAIT_MS
        and flush_await_ms <= _BOOTSTRAP_MAX_FLUSH_AWAIT_MS
        and average_queue_depth <= _BOOTSTRAP_MAX_AVERAGE_QUEUE_DEPTH
    )


def _bootstrap_clock() -> object | None:
    if "time" not in sys.builtin_module_names:
        return None
    try:
        return __import__("time")
    except BaseException:
        return None


def _wait_for_pre_import_safety(timeout_seconds: float) -> bool:
    """Fail-closed guard that runs before any non-bootstrap imports."""

    clock = _bootstrap_clock()
    if clock is None:
        return False
    device = _bootstrap_root_device()
    if device is None:
        return False
    try:
        started = clock.monotonic()
    except AttributeError:
        return False
    deadline = started + timeout_seconds
    previous = None
    safe_windows = 0
    while True:
        pressure = _bootstrap_io_pressure()
        memory = _bootstrap_memory()
        current = _bootstrap_disk_snapshot(clock, device)
        if pressure is None or memory is None or current is None:
            return False
        block_quiet = (
            False
            if previous is None
            else _bootstrap_block_window_is_quiet(previous, current)
        )
        if block_quiet is None:
            return False
        host_quiet = (
            pressure[0] <= _BOOTSTRAP_MAX_IO_PSI_AVG10
            and pressure[1] <= _BOOTSTRAP_MAX_IO_PSI_AVG60
            and memory[0] >= _BOOTSTRAP_MIN_MEM_AVAILABLE_KIB
            and memory[1] <= _BOOTSTRAP_MAX_SWAP_USED_KIB
        )
        if host_quiet and block_quiet:
            safe_windows += 1
            if safe_windows >= _BOOTSTRAP_REQUIRED_SAFE_WINDOWS:
                return True
        else:
            safe_windows = 0
        previous = current
        now = clock.monotonic()
        if now >= deadline:
            return False
        clock.sleep(min(_BOOTSTRAP_SAMPLE_SECONDS, deadline - now))


def _guarded_wait_for_pre_import_safety(timeout_seconds: float) -> bool:
    try:
        return _wait_for_pre_import_safety(timeout_seconds)
    except BaseException:
        return False


_DIRECT_BOOTSTRAP_TIMEOUT_SECONDS: float | None = None
if __name__ == "__main__":
    _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS = _bootstrap_float_option(
        "--admission-timeout-seconds",
        900.0,
    )
    if (
        _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS is None
        or __spec__ is not None
        or not sys.flags.no_site
        or not _BOOTSTRAP_STARTED_WITHOUT_BYTECODE_WRITES
        or not _guarded_wait_for_pre_import_safety(
            _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS
        )
    ):
        raise SystemExit(2)


import argparse
import importlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable


_SUPERVISOR_STAGED_MODULES = (
    "site",
    "hashlib",
    "secrets",
    "signal",
    "socket",
    "stat",
    "subprocess",
    "uuid",
    "dataclasses",
    "datetime",
    "scripts.testing.campaign_cgroup",
    "scripts.testing.audit_evidence_triad",
    "scripts.testing.campaign_comfyui_backend",
    "scripts.testing.campaign_control_channel",
    "scripts.testing.campaign_gate_bundle",
    "scripts.testing.campaign_source_freeze",
    "scripts.testing.campaign_observability",
    "scripts.testing.campaign_secret_scan",
    "scripts.testing.campaign_state",
    "scripts.testing.campaign_watchdog",
    "scripts.testing.campaign_runtime_contract",
    "scripts.testing.operational_campaign_runner_admission",
    "scripts.testing.operational_campaign_supervisor",
)


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.campaign_observability import (  # noqa: E402
    collect_host_startup_safety_preflight,
)


WAITABLE_DORMANT_IO_REASONS = frozenset({
    "HOST_IO_PRESSURE_HIGH",
    "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
})


def _load_supervisor_staged(timeout_seconds: float) -> object | None:
    """Load reviewed supervisor dependencies one at a time behind block gates."""

    for module_name in _SUPERVISOR_STAGED_MODULES:
        module = sys.modules.get(module_name)
        if module is None:
            if not _guarded_wait_for_pre_import_safety(timeout_seconds):
                return None
            try:
                module = importlib.import_module(module_name)
            except BaseException:
                return None
            if not _guarded_wait_for_pre_import_safety(timeout_seconds):
                return None
        if module_name == "site":
            getsitepackages = getattr(module, "getsitepackages", None)
            if not callable(getsitepackages):
                return None
            try:
                site_paths = getsitepackages()
            except BaseException:
                return None
            if (
                not isinstance(site_paths, list)
                or not site_paths
                or any(
                    not isinstance(path, str) or not path.startswith("/")
                    for path in site_paths
                )
            ):
                return None
            # Never call site.main(): .pth/sitecustomize/usercustomize code is
            # outside the reviewed staged-import profile.
            for path in site_paths:
                if path not in sys.path:
                    sys.path.append(path)
    supervisor = sys.modules.get(
        "scripts.testing.operational_campaign_supervisor"
    )
    if supervisor is None or not callable(getattr(supervisor, "main", None)):
        return None
    return supervisor


def wait_for_dormant_admission(
    *,
    timeout_seconds: float,
    poll_seconds: float = 1.0,
    required_consecutive_safe: int = 2,
    collector: Callable[[], dict[str, Any]] = (
        collect_host_startup_safety_preflight
    ),
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait for headroom before importing or creating campaign resources.

    I/O hard-limit evidence is waitable only in this dormant process.  Once
    the supervisor is exec'd, the same evidence is an immediate hard stop.
    """

    if timeout_seconds <= 0 or poll_seconds <= 0 or required_consecutive_safe < 1:
        raise ValueError("invalid dormant admission configuration")
    started = clock()
    safe_streak = 0
    sample_count = 0
    hard_io_samples = 0
    maximum_avg10 = 0.0
    maximum_avg60 = 0.0
    last: dict[str, Any] = {}
    while True:
        evidence = collector()
        sample_count += 1
        tripped = [str(item) for item in evidence.get("tripped") or ()]
        io_value = (
            (evidence.get("checks") or {})
            .get("host_io_pressure", {})
            .get("value", {})
        )
        avg10 = float(io_value.get("avg10") or 0.0)
        avg60 = float(io_value.get("avg60") or 0.0)
        maximum_avg10 = max(maximum_avg10, avg10)
        maximum_avg60 = max(maximum_avg60, avg60)
        if "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED" in tripped:
            hard_io_samples += 1
        last = {
            "at": evidence.get("at"),
            "ok": evidence.get("ok") is True,
            "tripped": tripped,
            "host_io_pressure": {"avg10": avg10, "avg60": avg60},
        }
        non_waitable = [
            reason
            for reason in tripped
            if reason not in WAITABLE_DORMANT_IO_REASONS
        ]
        if non_waitable:
            return {
                "ok": False,
                "reason": "NON_IO_HOST_SAFETY_FAILURE",
                "non_waitable": non_waitable,
                "sample_count": sample_count,
                "waited_seconds": round(max(0.0, clock() - started), 6),
                "last": last,
            }
        if evidence.get("ok") is True:
            safe_streak += 1
            if safe_streak >= required_consecutive_safe:
                return {
                    "ok": True,
                    "reason": "HOST_STARTUP_HEADROOM_AVAILABLE",
                    "sample_count": sample_count,
                    "required_consecutive_safe": required_consecutive_safe,
                    "hard_io_samples": hard_io_samples,
                    "maximum_io_pressure": {
                        "avg10": maximum_avg10,
                        "avg60": maximum_avg60,
                    },
                    "waited_seconds": round(max(0.0, clock() - started), 6),
                    "last": last,
                }
        else:
            safe_streak = 0
        elapsed = max(0.0, clock() - started)
        if elapsed >= timeout_seconds:
            return {
                "ok": False,
                "reason": "DORMANT_ADMISSION_TIMEOUT",
                "sample_count": sample_count,
                "hard_io_samples": hard_io_samples,
                "maximum_io_pressure": {
                    "avg10": maximum_avg10,
                    "avg60": maximum_avg60,
                },
                "waited_seconds": round(elapsed, 6),
                "last": last,
            }
        sleeper(min(poll_seconds, max(0.0, timeout_seconds - elapsed)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dormant host-safety admission for operational campaigns",
        add_help=False,
    )
    parser.add_argument("--admission-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--admission-poll-seconds", type=float, default=1.0)
    parser.add_argument("-h", "--help", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    _allow_unbootstrapped_for_tests: bool = False,
) -> int:
    if (
        _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS is None
        and not _allow_unbootstrapped_for_tests
    ):
        return 2
    if (
        _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS is not None
        and not _guarded_wait_for_pre_import_safety(
            _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS
        )
    ):
        return 2
    parser = build_parser()
    args, supervisor_args = parser.parse_known_args(argv)
    if args.help:
        if (
            _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS is not None
            and not sys.stdout.isatty()
        ):
            return 2
        parser.print_help()
        print("\nRemaining arguments are forwarded to operational_campaign_supervisor.py")
        return 0
    if not supervisor_args:
        if _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS is not None:
            return 2
        parser.error("supervisor arguments are required")
    pre_import = wait_for_dormant_admission(
        timeout_seconds=args.admission_timeout_seconds,
        poll_seconds=args.admission_poll_seconds,
    )
    pre_import["stage"] = "pre_supervisor_import"
    if _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS is None:
        print(json.dumps(pre_import, sort_keys=True), flush=True)
    if pre_import.get("ok") is not True:
        return 2
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ.pop("PYTHONPYCACHEPREFIX", None)
    if _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS is not None:
        supervisor = _load_supervisor_staged(
            _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS
        )
        if supervisor is None:
            return 2
    else:
        supervisor = importlib.import_module(
            "scripts.testing.operational_campaign_supervisor"
        )
    if (
        _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS is not None
        and not _guarded_wait_for_pre_import_safety(
            _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS
        )
    ):
        return 2
    post_import = wait_for_dormant_admission(
        timeout_seconds=args.admission_timeout_seconds,
        poll_seconds=args.admission_poll_seconds,
    )
    post_import["stage"] = "post_supervisor_import"
    if _DIRECT_BOOTSTRAP_TIMEOUT_SECONDS is None:
        print(json.dumps(post_import, sort_keys=True), flush=True)
    if post_import.get("ok") is not True:
        return 2
    return int(supervisor.main(supervisor_args))


if __name__ == "__main__":
    raise SystemExit(main())
