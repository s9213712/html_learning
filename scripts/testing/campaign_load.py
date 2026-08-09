#!/usr/bin/env python3
"""Effective-load evidence for the formal campaign.

Configured concurrency is recorded, but never treated as proof that useful
work happened.  Target coverage requires active workers and measured
throughput, with maintenance exclusions carrying explicit reason codes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


LOAD_SAMPLE_SCHEMA_VERSION = "hackme.effective-load.v1"
ALLOWED_MAINTENANCE_REASONS = frozenset({
    "PLANNED_RESTART",
    "BACKUP_RESTORE",
    "HEAVY_JOB_COORDINATION",
    "SECURITY_SENTINEL_VALIDATION",
})
DEGRADE_REASON_CODES = frozenset({
    "LATENCY_HIGH",
    "MEMORY_PRESSURE",
    "IO_PRESSURE",
    "GPU_PRESSURE",
    "DB_LOCK_PRESSURE",
    "DISK_LOW",
    "MANUAL_SAFETY_STOP",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _finite_nonnegative(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return float(value)


@dataclass(frozen=True)
class EffectiveLoadWindow:
    window_started_at: str
    window_seconds: float
    scheduled_load_level: int
    active_workers: int
    inflight_requests: int
    operations_completed: int
    expected_operations: float
    blocked_workers: int
    idle_workers: int
    queue_depth: int
    retries: int
    attempts: int
    baseline_32_operations_per_minute: float
    maintenance_window: bool = False
    maintenance_reason: str = ""
    degradation_reason: str = ""
    target_load_level: int = 32
    minimum_active_workers_at_target: int = 28

    def __post_init__(self) -> None:
        _finite_nonnegative(self.window_seconds, "window_seconds")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        _finite_nonnegative(self.expected_operations, "expected_operations")
        _finite_nonnegative(self.baseline_32_operations_per_minute, "baseline_32_operations_per_minute")
        integer_fields = (
            "scheduled_load_level",
            "active_workers",
            "inflight_requests",
            "operations_completed",
            "blocked_workers",
            "idle_workers",
            "queue_depth",
            "retries",
            "attempts",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.retries > self.attempts:
            raise ValueError("retries cannot exceed attempts")
        if self.maintenance_window:
            if self.maintenance_reason not in ALLOWED_MAINTENANCE_REASONS:
                raise ValueError("maintenance window requires an allowed reason")
        elif self.maintenance_reason:
            raise ValueError("maintenance_reason requires maintenance_window=true")
        if self.degradation_reason and self.degradation_reason not in DEGRADE_REASON_CODES:
            raise ValueError("unknown degradation_reason")
        if self.target_load_level <= 0:
            raise ValueError("target_load_level must be a positive integer")
        if self.minimum_active_workers_at_target <= 0:
            raise ValueError("minimum_active_workers_at_target must be a positive integer")
        if self.minimum_active_workers_at_target > self.target_load_level:
            raise ValueError("minimum_active_workers_at_target cannot exceed target_load_level")

    def evidence(self) -> dict[str, Any]:
        throughput = self.operations_completed / self.window_seconds * 60
        effective_ratio = self.operations_completed / self.expected_operations if self.expected_operations > 0 else 0.0
        retry_rate = self.retries / self.attempts if self.attempts > 0 else 0.0
        target_scheduled = self.scheduled_load_level == self.target_load_level
        enough_workers = self.active_workers >= self.minimum_active_workers_at_target
        throughput_ok = self.baseline_32_operations_per_minute > 0 and throughput >= self.baseline_32_operations_per_minute * 0.80
        effective_ratio_ok = effective_ratio >= 0.85
        at_target = bool(
            not self.maintenance_window
            and target_scheduled
            and enough_workers
            and throughput_ok
            and effective_ratio_ok
            and not self.degradation_reason
        )
        failures: list[str] = []
        if not self.maintenance_window:
            if not target_scheduled:
                failures.append(f"SCHEDULED_LOAD_NOT_{self.target_load_level}")
            if not enough_workers:
                failures.append(
                    f"ACTIVE_WORKERS_BELOW_{self.minimum_active_workers_at_target}"
                )
            if not throughput_ok:
                failures.append("THROUGHPUT_BELOW_BASELINE_80_PERCENT")
            if not effective_ratio_ok:
                failures.append("EFFECTIVE_LOAD_RATIO_BELOW_0_85")
            if self.degradation_reason:
                failures.append(self.degradation_reason)
        return {
            "sample_schema_version": LOAD_SAMPLE_SCHEMA_VERSION,
            "sampled_at": utc_now(),
            "window_started_at": self.window_started_at,
            "window_seconds": self.window_seconds,
            "scheduled_load_level": self.scheduled_load_level,
            "active_workers": self.active_workers,
            "inflight_requests": self.inflight_requests,
            "operations_completed": self.operations_completed,
            "expected_operations": self.expected_operations,
            "operations_per_minute": round(throughput, 6),
            "baseline_32_operations_per_minute": self.baseline_32_operations_per_minute,
            "baseline_target_operations_per_minute": self.baseline_32_operations_per_minute,
            "target_load_level": self.target_load_level,
            "minimum_active_workers_at_target": self.minimum_active_workers_at_target,
            "effective_load_ratio": round(effective_ratio, 6),
            "blocked_workers": self.blocked_workers,
            "idle_workers": self.idle_workers,
            "queue_depth": self.queue_depth,
            "retry_rate": round(retry_rate, 6),
            "maintenance_window": self.maintenance_window,
            "maintenance_reason": self.maintenance_reason,
            "degradation_reason": self.degradation_reason,
            "target_conditions": {
                f"scheduled_load_level_{self.target_load_level}": target_scheduled,
                f"active_workers_at_least_{self.minimum_active_workers_at_target}": enough_workers,
                "throughput_at_least_baseline_80_percent": throughput_ok,
                "effective_load_ratio_at_least_0_85": effective_ratio_ok,
            },
            "target_failure_reasons": failures,
            "at_target_load": at_target,
        }


def summarize_target_load(
    samples: Iterable[dict[str, Any]],
    *,
    minimum_coverage: float = 0.90,
    target_load_level: int = 32,
) -> dict[str, Any]:
    rows = list(samples)
    eligible_seconds = 0.0
    target_seconds = 0.0
    maintenance_seconds = 0.0
    invalid_samples: list[int] = []
    for index, row in enumerate(rows):
        if row.get("sample_schema_version") != LOAD_SAMPLE_SCHEMA_VERSION:
            invalid_samples.append(index)
            continue
        try:
            seconds = float(row.get("window_seconds"))
        except (TypeError, ValueError):
            invalid_samples.append(index)
            continue
        if not math.isfinite(seconds) or seconds <= 0:
            invalid_samples.append(index)
            continue
        if row.get("maintenance_window"):
            if row.get("maintenance_reason") not in ALLOWED_MAINTENANCE_REASONS:
                invalid_samples.append(index)
            else:
                maintenance_seconds += seconds
            continue
        # Ramp windows below the configured target are intentionally outside
        # the post-ramp target denominator.  A later unexpected drop below the
        # target must carry a
        # degradation reason and therefore remains eligible/failing.
        if (
            int(row.get("scheduled_load_level") or 0) < int(target_load_level)
            and not row.get("degradation_reason")
        ):
            continue
        eligible_seconds += seconds
        if row.get("at_target_load") is True:
            target_seconds += seconds
    coverage = target_seconds / eligible_seconds if eligible_seconds > 0 else 0.0
    return {
        "sample_schema_version": LOAD_SAMPLE_SCHEMA_VERSION,
        "samples": len(rows),
        "eligible_post_ramp_seconds": round(eligible_seconds, 6),
        "target_load_seconds": round(target_seconds, 6),
        "maintenance_seconds_excluded": round(maintenance_seconds, 6),
        "target_load_coverage": round(coverage, 6),
        "minimum_required_coverage": float(minimum_coverage),
        "target_load_level": int(target_load_level),
        "invalid_samples": invalid_samples,
        "ok": bool(rows) and eligible_seconds > 0 and coverage >= minimum_coverage and not invalid_samples,
    }
