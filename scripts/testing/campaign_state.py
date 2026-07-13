#!/usr/bin/env python3
"""Durable fail-closed state machine for the operational campaign harness.

The formal timer is deliberately owned by this state file rather than by the
orchestrator's wall clock.  Every update is protected by an advisory file lock
and committed with atomic replace so an external watchdog can safely inspect or
change load-admission state without sharing Python memory with the campaign.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


STATE_SCHEMA_VERSION = "hackme.campaign-state.v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class CampaignState(str, Enum):
    PREPARING = "PREPARING"
    PREFLIGHT = "PREFLIGHT"
    FROZEN = "FROZEN"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    STOPPING_LOAD = "STOPPING_LOAD"
    PRESERVING_EVIDENCE = "PRESERVING_EVIDENCE"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    AUDITING = "AUDITING"
    PASS = "PASS"


TERMINAL_STATES = frozenset({CampaignState.INTERRUPTED, CampaignState.FAILED, CampaignState.PASS})


ALLOWED_TRANSITIONS: dict[CampaignState, frozenset[CampaignState]] = {
    CampaignState.PREPARING: frozenset({CampaignState.PREFLIGHT, CampaignState.FAILED, CampaignState.INTERRUPTED}),
    CampaignState.PREFLIGHT: frozenset({CampaignState.FROZEN, CampaignState.FAILED, CampaignState.INTERRUPTED}),
    CampaignState.FROZEN: frozenset({CampaignState.ACTIVE, CampaignState.STOPPING_LOAD, CampaignState.FAILED, CampaignState.INTERRUPTED}),
    CampaignState.ACTIVE: frozenset({CampaignState.DEGRADED, CampaignState.STOPPING_LOAD, CampaignState.COMPLETED}),
    CampaignState.DEGRADED: frozenset({CampaignState.ACTIVE, CampaignState.STOPPING_LOAD, CampaignState.COMPLETED}),
    CampaignState.STOPPING_LOAD: frozenset({CampaignState.PRESERVING_EVIDENCE}),
    CampaignState.PRESERVING_EVIDENCE: frozenset({CampaignState.INTERRUPTED, CampaignState.FAILED}),
    CampaignState.COMPLETED: frozenset({CampaignState.AUDITING, CampaignState.FAILED}),
    CampaignState.AUDITING: frozenset({CampaignState.PASS, CampaignState.FAILED}),
    CampaignState.INTERRUPTED: frozenset(),
    CampaignState.FAILED: frozenset(),
    CampaignState.PASS: frozenset(),
}


class CampaignStateError(RuntimeError):
    """The requested state mutation would weaken formal-run guarantees."""


class AtomicStateStore:
    """Cross-process JSON state store using flock plus atomic replace."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CampaignStateError(f"campaign state is not initialized: {self.path}") from exc
        except Exception as exc:
            raise CampaignStateError(f"campaign state is unreadable: {exc.__class__.__name__}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != STATE_SCHEMA_VERSION:
            raise CampaignStateError("campaign state schema is missing or unsupported")
        return payload

    def read(self) -> dict[str, Any]:
        with self._locked():
            return copy.deepcopy(self._read_unlocked())

    def initialize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._locked():
            if self.path.exists():
                raise CampaignStateError(f"refusing to overwrite existing campaign state: {self.path}")
            state = copy.deepcopy(dict(payload))
            state["schema_version"] = STATE_SCHEMA_VERSION
            state["revision"] = 1
            state["updated_at"] = utc_now()
            self._write_unlocked(state)
            return copy.deepcopy(state)

    def update(self, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._locked():
            state = self._read_unlocked()
            mutate(state)
            state["schema_version"] = STATE_SCHEMA_VERSION
            state["revision"] = int(state.get("revision") or 0) + 1
            state["updated_at"] = utc_now()
            self._write_unlocked(state)
            return copy.deepcopy(state)

    def _write_unlocked(self, payload: Mapping[str, Any]) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)


class CampaignStateMachine:
    """Formal campaign state, timer, heartbeat, and load-admission authority."""

    def __init__(self, path: Path):
        self.store = AtomicStateStore(path)

    def initialize(
        self,
        *,
        campaign_uuid: str,
        required_active_seconds: int,
        orchestrator_pid: int,
        orchestrator_start_ticks: int,
    ) -> dict[str, Any]:
        if not campaign_uuid or int(required_active_seconds) <= 0:
            raise CampaignStateError("campaign UUID and positive duration are required")
        now_ns = time.monotonic_ns()
        now = utc_now()
        return self.store.initialize({
            "campaign_uuid": str(campaign_uuid),
            "state": CampaignState.PREPARING.value,
            "classification": None,
            "reason": "campaign_initialized",
            "state_entered_at": now,
            "control": {
                "admit_new_jobs": False,
                "load_generator_should_run": False,
                "preserve_evidence_requested": False,
            },
            "clock": {
                "required_active_seconds": int(required_active_seconds),
                "active_started_at": None,
                "active_finished_at": None,
                "wall_clock_seconds": 0.0,
                "continuous_active_seconds": 0.0,
                "invalid_seconds": 0.0,
                "last_valid_active_at": None,
                "clock_pause_reason": "not_started",
                "formal_segment_valid": True,
                "last_tick_monotonic_ns": now_ns,
                "conditions": {},
            },
            "heartbeat": {
                "orchestrator_pid": int(orchestrator_pid),
                "orchestrator_start_ticks": int(orchestrator_start_ticks),
                "orchestrator_at": now,
                "orchestrator_monotonic_ns": now_ns,
                "watchdog_pid": 0,
                "watchdog_at": None,
            },
            "hard_stop": None,
            "events": [{"at": now, "state": CampaignState.PREPARING.value, "reason": "campaign_initialized"}],
        })

    def snapshot(self) -> dict[str, Any]:
        return self.store.read()

    @staticmethod
    def _state(payload: Mapping[str, Any]) -> CampaignState:
        try:
            return CampaignState(str(payload.get("state") or ""))
        except ValueError as exc:
            raise CampaignStateError(f"unknown campaign state: {payload.get('state')!r}") from exc

    @staticmethod
    def _append_event(payload: dict[str, Any], *, state: CampaignState, reason: str, evidence: Any = None) -> None:
        events = payload.setdefault("events", [])
        event: dict[str, Any] = {"at": utc_now(), "state": state.value, "reason": str(reason)}
        if evidence is not None:
            event["evidence"] = evidence
        events.append(event)
        if len(events) > 500:
            del events[:-500]

    def transition(
        self,
        target: CampaignState,
        *,
        reason: str,
        classification: str | None = None,
        evidence: Any = None,
    ) -> dict[str, Any]:
        target = CampaignState(target)

        def mutate(payload: dict[str, Any]) -> None:
            current = self._state(payload)
            if target not in ALLOWED_TRANSITIONS[current]:
                raise CampaignStateError(f"invalid campaign transition: {current.value} -> {target.value}")
            payload["state"] = target.value
            payload["state_entered_at"] = utc_now()
            payload["reason"] = str(reason)
            if classification is not None:
                payload["classification"] = str(classification)
            control = payload.setdefault("control", {})
            if target in {CampaignState.STOPPING_LOAD, CampaignState.PRESERVING_EVIDENCE, *TERMINAL_STATES}:
                control["admit_new_jobs"] = False
                control["load_generator_should_run"] = False
            if target == CampaignState.PRESERVING_EVIDENCE:
                control["preserve_evidence_requested"] = True
            self._append_event(payload, state=target, reason=reason, evidence=evidence)

        return self.store.update(mutate)

    def mark_frozen(self, *, source: Mapping[str, Any], containment: Mapping[str, Any]) -> dict[str, Any]:
        if not source.get("verified") or not containment.get("verified"):
            raise CampaignStateError("source and containment must be machine-verified before FROZEN")

        def attach(payload: dict[str, Any]) -> None:
            if self._state(payload) != CampaignState.PREFLIGHT:
                raise CampaignStateError("source can only be frozen after PREFLIGHT")
            payload["source_freeze"] = copy.deepcopy(dict(source))
            payload["containment"] = copy.deepcopy(dict(containment))
            payload["state"] = CampaignState.FROZEN.value
            payload["state_entered_at"] = utc_now()
            payload["reason"] = "source_and_containment_verified"
            self._append_event(payload, state=CampaignState.FROZEN, reason=payload["reason"])

        return self.store.update(attach)

    def start_active(self, conditions: Mapping[str, bool], *, now_ns: int | None = None) -> dict[str, Any]:
        normalized = {str(name): bool(value) for name, value in conditions.items()}
        failed = sorted(name for name, value in normalized.items() if not value)
        if not normalized or failed:
            raise CampaignStateError("formal ACTIVE conditions are not all true: " + ", ".join(failed or ["missing_conditions"]))
        tick_ns = int(now_ns if now_ns is not None else time.monotonic_ns())

        def mutate(payload: dict[str, Any]) -> None:
            if self._state(payload) != CampaignState.FROZEN:
                raise CampaignStateError("formal timer can only start from FROZEN")
            now = utc_now()
            payload["state"] = CampaignState.ACTIVE.value
            payload["state_entered_at"] = now
            payload["reason"] = "all_active_conditions_verified"
            payload["control"] = {
                "admit_new_jobs": True,
                "load_generator_should_run": True,
                "preserve_evidence_requested": False,
            }
            clock = payload["clock"]
            clock.update({
                "active_started_at": now,
                "active_finished_at": None,
                "wall_clock_seconds": 0.0,
                "continuous_active_seconds": 0.0,
                "invalid_seconds": 0.0,
                "last_valid_active_at": now,
                "clock_pause_reason": None,
                "formal_segment_valid": True,
                "last_tick_monotonic_ns": tick_ns,
                "conditions": normalized,
            })
            self._append_event(payload, state=CampaignState.ACTIVE, reason=payload["reason"])

        return self.store.update(mutate)

    def tick_active(self, conditions: Mapping[str, bool], *, now_ns: int | None = None) -> dict[str, Any]:
        normalized = {str(name): bool(value) for name, value in conditions.items()}
        tick_ns = int(now_ns if now_ns is not None else time.monotonic_ns())

        def mutate(payload: dict[str, Any]) -> None:
            state = self._state(payload)
            if state not in {CampaignState.ACTIVE, CampaignState.DEGRADED}:
                raise CampaignStateError(f"cannot advance active clock in {state.value}")
            clock = payload["clock"]
            previous_ns = int(clock.get("last_tick_monotonic_ns") or tick_ns)
            if tick_ns < previous_ns:
                raise CampaignStateError("monotonic clock moved backwards")
            delta = (tick_ns - previous_ns) / 1_000_000_000
            clock["last_tick_monotonic_ns"] = tick_ns
            clock["wall_clock_seconds"] = round(float(clock.get("wall_clock_seconds") or 0.0) + delta, 6)
            previous_conditions = {str(k): bool(v) for k, v in (clock.get("conditions") or {}).items()}
            failed = sorted(name for name, value in normalized.items() if not value)
            all_valid = bool(normalized) and not failed and bool(previous_conditions) and all(previous_conditions.values())
            if bool(clock.get("formal_segment_valid")) and all_valid:
                clock["continuous_active_seconds"] = round(
                    float(clock.get("continuous_active_seconds") or 0.0) + delta,
                    6,
                )
                clock["last_valid_active_at"] = utc_now()
                clock["clock_pause_reason"] = None
            else:
                clock["invalid_seconds"] = round(float(clock.get("invalid_seconds") or 0.0) + delta, 6)
                clock["formal_segment_valid"] = False
                clock["clock_pause_reason"] = ",".join(failed or ["previous_sample_invalid_or_missing"])
                payload["control"]["admit_new_jobs"] = False
                payload["control"]["load_generator_should_run"] = False
            clock["conditions"] = normalized

        return self.store.update(mutate)

    def heartbeat(
        self,
        *,
        orchestrator_pid: int,
        orchestrator_start_ticks: int,
        checkpoint_revision: int,
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        tick_ns = int(now_ns if now_ns is not None else time.monotonic_ns())

        def mutate(payload: dict[str, Any]) -> None:
            heartbeat = payload.setdefault("heartbeat", {})
            expected_pid = int(heartbeat.get("orchestrator_pid") or 0)
            expected_ticks = int(heartbeat.get("orchestrator_start_ticks") or 0)
            if expected_pid and expected_pid != int(orchestrator_pid):
                raise CampaignStateError("orchestrator PID identity changed")
            if expected_ticks and expected_ticks != int(orchestrator_start_ticks):
                raise CampaignStateError("orchestrator starttime identity changed")
            heartbeat.update({
                "orchestrator_pid": int(orchestrator_pid),
                "orchestrator_start_ticks": int(orchestrator_start_ticks),
                "orchestrator_at": utc_now(),
                "orchestrator_monotonic_ns": tick_ns,
                "checkpoint_revision": int(checkpoint_revision),
            })

        return self.store.update(mutate)

    def hard_stop(
        self,
        *,
        reason_code: str,
        classification: str,
        evidence: Mapping[str, Any],
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        if not reason_code or not classification:
            raise CampaignStateError("hard stop requires reason code and classification")
        tick_ns = int(now_ns if now_ns is not None else time.monotonic_ns())

        def mutate(payload: dict[str, Any]) -> None:
            current = self._state(payload)
            if current not in {CampaignState.FROZEN, CampaignState.ACTIVE, CampaignState.DEGRADED}:
                raise CampaignStateError(f"hard stop is not valid from {current.value}")
            now = utc_now()
            clock = payload["clock"]
            # Fail closed: do not credit the interval between the last good
            # observation and detection of the hard stop.
            previous_ns = int(clock.get("last_tick_monotonic_ns") or tick_ns)
            if tick_ns >= previous_ns and clock.get("active_started_at"):
                delta = (tick_ns - previous_ns) / 1_000_000_000
                clock["wall_clock_seconds"] = round(float(clock.get("wall_clock_seconds") or 0.0) + delta, 6)
                clock["invalid_seconds"] = round(float(clock.get("invalid_seconds") or 0.0) + delta, 6)
            clock.update({
                "last_tick_monotonic_ns": tick_ns,
                "active_finished_at": now,
                "formal_segment_valid": False,
                "clock_pause_reason": str(reason_code),
            })
            payload["state"] = CampaignState.STOPPING_LOAD.value
            payload["state_entered_at"] = now
            payload["classification"] = str(classification)
            payload["reason"] = str(reason_code)
            payload["control"] = {
                "admit_new_jobs": False,
                "load_generator_should_run": False,
                "preserve_evidence_requested": True,
            }
            payload["hard_stop"] = {
                "at": now,
                "reason_code": str(reason_code),
                "classification": str(classification),
                "evidence": copy.deepcopy(dict(evidence)),
            }
            self._append_event(payload, state=CampaignState.STOPPING_LOAD, reason=reason_code, evidence=evidence)

        return self.store.update(mutate)

    def finish_active(self, *, now_ns: int | None = None) -> dict[str, Any]:
        tick_ns = int(now_ns if now_ns is not None else time.monotonic_ns())

        def mutate(payload: dict[str, Any]) -> None:
            current = self._state(payload)
            if current not in {CampaignState.ACTIVE, CampaignState.DEGRADED}:
                raise CampaignStateError(f"cannot complete campaign from {current.value}")
            clock = payload["clock"]
            if not clock.get("formal_segment_valid"):
                raise CampaignStateError("invalidated formal segment cannot become COMPLETED")
            previous_ns = int(clock.get("last_tick_monotonic_ns") or tick_ns)
            if tick_ns < previous_ns:
                raise CampaignStateError("monotonic clock moved backwards")
            delta = (tick_ns - previous_ns) / 1_000_000_000
            clock["wall_clock_seconds"] = round(float(clock.get("wall_clock_seconds") or 0.0) + delta, 6)
            clock["continuous_active_seconds"] = round(float(clock.get("continuous_active_seconds") or 0.0) + delta, 6)
            clock["last_tick_monotonic_ns"] = tick_ns
            clock["active_finished_at"] = utc_now()
            if float(clock["continuous_active_seconds"]) + 1e-6 < int(clock["required_active_seconds"]):
                raise CampaignStateError("required continuous active duration has not completed")
            payload["state"] = CampaignState.COMPLETED.value
            payload["state_entered_at"] = utc_now()
            payload["reason"] = "required_continuous_duration_completed"
            payload["control"]["admit_new_jobs"] = False
            payload["control"]["load_generator_should_run"] = False
            self._append_event(payload, state=CampaignState.COMPLETED, reason=payload["reason"])

        return self.store.update(mutate)


def process_start_ticks(pid: int) -> int:
    """Return Linux /proc starttime ticks, which disambiguate PID reuse."""

    try:
        tail = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
        return int(tail[19])
    except Exception as exc:
        raise CampaignStateError(f"cannot verify process identity for pid {pid}: {exc.__class__.__name__}: {exc}") from exc

