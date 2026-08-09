#!/usr/bin/env python3
"""Lightweight shared runtime contract for campaign launchers and runners.

This module intentionally has no application or probe imports.  The outside
supervisor must be able to validate host safety before loading the much larger
24-hour runner module.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MIN_FORMAL_SECONDS = 24 * 60 * 60

SUPERVISED_LEVEL_DURATIONS = {
    "smoke": 180,
    "rehearsal": 3_600,
    "soak": MIN_FORMAL_SECONDS,
    "formal": MIN_FORMAL_SECONDS,
}

SUPERVISED_RUNNER_PROFILES: dict[str, dict[str, int | float]] = {
    level: {
        # SQLite has a single writer across the whole database.  A single
        # gthread process keeps write arbitration process-local while still
        # leaving enough lanes for the verified 128-way business workload.
        "workers": 1,
        "threads": 160,
        "account_count": 10,
        "round_ops": 1_000,
        "concurrency": 128,
        "session_pool": 128,
        "browser_interval_seconds": 3 * 60 * 60,
        "resource_interval": 5.0,
        "heartbeat_interval": 60.0,
        "scenario_join_timeout_seconds": 8 * 60 * 60,
        "minimum_free_gb": 20.0,
        "max_server_busy_rate": 0.01,
        "max_ordinary_p95_ms": 30_000.0,
        "max_ordinary_p99_ms": 60_000.0,
        "max_sentinel_p95_ms": 3_000.0,
    }
    for level in SUPERVISED_LEVEL_DURATIONS
}

SUPERVISED_RUNNER_PROFILES["smoke"].update({
    "workers": 1,
    "threads": 2,
    "account_count": 2,
    "round_ops": 50,
    "concurrency": 2,
    "session_pool": 2,
    "resource_interval": 2.0,
    "heartbeat_interval": 30.0,
})

SUPERVISED_RUNNER_PROFILES["soak"].update({
    "workers": 1,
    "threads": 16,
    "account_count": 4,
    "round_ops": 250,
    "concurrency": 4,
    "session_pool": 4,
    "resource_interval": 2.0,
    "heartbeat_interval": 30.0,
})

SUPERVISED_RUNNER_PROFILE_OPTIONS = {
    "workers": "--workers",
    "threads": "--threads",
    "account_count": "--account-count",
    "round_ops": "--round-ops",
    "concurrency": "--concurrency",
    "session_pool": "--session-pool",
    "browser_interval_seconds": "--browser-interval-seconds",
    "resource_interval": "--resource-interval",
    "heartbeat_interval": "--heartbeat-interval",
    "scenario_join_timeout_seconds": "--scenario-join-timeout-seconds",
    "minimum_free_gb": "--minimum-free-gb",
    "max_server_busy_rate": "--max-server-busy-rate",
    "max_ordinary_p95_ms": "--max-ordinary-p95-ms",
    "max_ordinary_p99_ms": "--max-ordinary-p99-ms",
    "max_sentinel_p95_ms": "--max-sentinel-p95-ms",
}

SUPERVISED_LOAD_POLICIES: dict[str, dict[str, Any]] = {
    level: {
        "ramp_required": level in {"rehearsal", "formal"},
        "ramp_levels": [4, 8, 16, 32, 64, 128],
        "minimum_ramp_stage_seconds": (
            {"4": 360.0, "8": 540.0, "16": 720.0, "32": 900.0, "64": 1_080.0, "128": 0.0}
            if level == "formal"
            else {"4": 36.0, "8": 54.0, "16": 72.0, "32": 90.0, "64": 108.0, "128": 0.0}
            if level == "rehearsal"
            else {"4": 0.0, "8": 0.0, "16": 0.0, "32": 0.0, "64": 0.0, "128": 0.0}
        ),
        "minimum_target_load_coverage": 0.90,
        "target_load_level": 128,
        "minimum_active_workers_at_target": 109,
        "minimum_baseline_throughput_ratio": 0.80,
        "minimum_effective_operation_ratio": 0.85,
        "maximum_stage_boundary_lag_seconds": 15.0,
        "ramp_completion_deadline_seconds": (
            3_600.0 if level == "formal" else 360.0 if level == "rehearsal" else 0.0
        ),
        "minimum_post_ramp_seconds": (
            82_800.0 if level == "formal" else 3_240.0 if level == "rehearsal" else 0.0
        ),
    }
    for level in SUPERVISED_LEVEL_DURATIONS
}


def validate_tmp_path(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    tmp = Path("/tmp").resolve()
    if resolved != tmp and tmp not in resolved.parents:
        raise ValueError(f"{label} must remain under /tmp: {resolved}")
    return resolved


def validate_control_root(campaign_root: Path, control_root: Path) -> Path:
    """Require the live control plane to be a private sibling of artifacts."""

    campaign = validate_tmp_path(campaign_root, label="campaign root")
    control = validate_tmp_path(control_root, label="campaign control root")
    if control == Path("/tmp").resolve() or control == campaign:
        raise ValueError("campaign control root must be a distinct directory below /tmp")
    if control.parent != campaign.parent:
        raise ValueError("campaign control root must be a sibling of campaign root")
    return control


@dataclass(frozen=True)
class Credentials:
    root: str
    manager: str
    test: str
    member: str

    @classmethod
    def load(cls, *, managed_servers: bool) -> "Credentials":
        names = {
            "root": "HACKME_CAMPAIGN_ROOT_PASSWORD",
            "manager": "HACKME_CAMPAIGN_MANAGER_PASSWORD",
            "test": "HACKME_CAMPAIGN_TEST_PASSWORD",
            "member": "HACKME_CAMPAIGN_MEMBER_PASSWORD",
        }
        values = {key: str(os.environ.get(name) or "") for key, name in names.items()}
        if not managed_servers:
            missing = [name for key, name in names.items() if not values[key]]
            if missing:
                raise ValueError(
                    "existing targets require credential environment variables: "
                    + ", ".join(missing)
                )
        for key in values:
            values[key] = values[key] or f"Campaign-{key}-{secrets.token_urlsafe(24)}"
        return cls(**values)

    def child_env(self) -> dict[str, str]:
        return {
            "HACKME_PROBE_ROOT_PASSWORD": self.root,
            "HACKME_PROBE_MANAGER_PASSWORD": self.manager,
            "HACKME_PROBE_USER_PASSWORD": self.test,
            "HACKME_ROOT_PASSWORD": self.root,
            "HACKME_MANAGER_PASSWORD": self.manager,
            "HACKME_TEST_PASSWORD": self.test,
            "PLAYWRIGHT_ROOT_PASSWORD": self.root,
            "PLAYWRIGHT_MANAGER_PASSWORD": self.manager,
            "PLAYWRIGHT_TEST_PASSWORD": self.test,
            "PENTEST_ROOT_PASSWORD": self.root,
            "PENTEST_MANAGER_PASSWORD": self.manager,
            "PENTEST_TEST_PASSWORD": self.test,
            "PENTEST_STRESS_USER_PASSWORD": self.member,
            "HACKME_QA_ROOT_PASSWORD": self.root,
            "HACKME_QA_TEST_PASSWORD": self.test,
            "HACKME_TRADING_PROBE_USER_PASSWORD": self.member,
        }
