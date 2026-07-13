"""Fail-closed scenario contracts and formal campaign result taxonomy.

This module deliberately does not run scenarios.  It gives the campaign
orchestrator a strict, serialisable boundary between the reviewed test design
and the evidence produced at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping


SCENARIO_CONTRACT_SCHEMA_VERSION = "hackme.campaign.scenario-contract/v2"
SCENARIO_RESULT_SCHEMA_VERSION = "hackme.campaign.scenario-result/v2"
SCENARIO_ROLLUP_SCHEMA_VERSION = "hackme.campaign.scenario-rollup/v1"
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,127}$")


class ContractValidationError(ValueError):
    """Raised when a reviewed scenario contract is incomplete or ambiguous."""


class FormalResultStatus(str, Enum):
    """Diagnostic result taxonomy; only :attr:`PASS` is a formal pass."""

    PASS = "PASS"
    FAIL_PRODUCT = "FAIL_PRODUCT"
    FAIL_HARNESS = "FAIL_HARNESS"
    FAIL_INFRA = "FAIL_INFRA"
    FAIL_EXTERNAL = "FAIL_EXTERNAL"
    BLOCKED = "BLOCKED"
    INVALIDATED = "INVALIDATED"
    INTERRUPTED = "INTERRUPTED"

    @property
    def is_pass(self) -> bool:
        return self is FormalResultStatus.PASS


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{label} must be a non-empty string")
    return value.strip()


def _identifier(value: object, label: str) -> str:
    text = _required_text(value, label)
    if not _IDENTIFIER.fullmatch(text):
        raise ContractValidationError(
            f"{label} must match {_IDENTIFIER.pattern!r}: {text!r}"
        )
    return text


def _string_tuple(
    values: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ContractValidationError(f"{label} must be a sequence of strings")
    result: list[str] = []
    for index, value in enumerate(values):
        result.append(_required_text(value, f"{label}[{index}]"))
    if not allow_empty and not result:
        raise ContractValidationError(f"{label} must not be empty")
    if len(set(result)) != len(result):
        raise ContractValidationError(f"{label} must not contain duplicates")
    return tuple(result)


def _identifier_tuple(
    values: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    result = _string_tuple(values, label, allow_empty=allow_empty)
    for index, value in enumerate(result):
        _identifier(value, f"{label}[{index}]")
    return result


def _finite_number(
    value: object,
    label: str,
    *,
    allow_zero: bool,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (float(value) < 0 if allow_zero else float(value) <= 0)
    ):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ContractValidationError(f"{label} must be a finite {qualifier} number")
    return float(value)


def _preferred_window(value: object) -> tuple[float, float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ContractValidationError("preferred_window must be a two-number sequence")
    items = tuple(value)
    if len(items) != 2:
        raise ContractValidationError("preferred_window must contain exactly start and end")
    start = _finite_number(items[0], "preferred_window.start", allow_zero=True)
    end = _finite_number(items[1], "preferred_window.end", allow_zero=False)
    if end <= start:
        raise ContractValidationError("preferred_window.end must be greater than start")
    return (start, end)


def _assertion_map(values: object, label: str) -> Mapping[str, bool]:
    if not isinstance(values, Mapping):
        raise ContractValidationError(f"{label} must be an object of boolean assertions")
    normalized: dict[str, bool] = {}
    for key, value in values.items():
        name = _required_text(key, f"{label} key")
        if type(value) is not bool:
            raise ContractValidationError(f"{label}.{name} must be boolean")
        normalized[name] = value
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class ScenarioContract:
    """Reviewed execution and evidence contract for one scenario.

    Scheduling values are elapsed seconds from the campaign's ACTIVE boundary.
    ``preferred_window`` constrains preferred start time; ``hard_deadline`` is
    the latest permitted terminal completion time.
    """

    scenario_id: str
    domain: str
    mandatory: bool
    role: str
    preconditions: tuple[str, ...]
    steps: tuple[str, ...]
    expected_terminal_state: str
    side_effect_assertions: tuple[str, ...]
    cleanup_assertions: tuple[str, ...]
    artifacts: tuple[str, ...]
    deadline_seconds: float
    earliest_start: float
    preferred_window: tuple[float, float]
    hard_deadline: float
    resource_class: tuple[str, ...]
    conflicts_with: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", _identifier(self.scenario_id, "scenario_id"))
        object.__setattr__(self, "domain", _identifier(self.domain, "domain"))
        if type(self.mandatory) is not bool:
            raise ContractValidationError("mandatory must be boolean")
        object.__setattr__(self, "role", _required_text(self.role, "role"))
        object.__setattr__(
            self,
            "preconditions",
            _string_tuple(self.preconditions, "preconditions", allow_empty=True),
        )
        object.__setattr__(self, "steps", _string_tuple(self.steps, "steps"))
        object.__setattr__(
            self,
            "expected_terminal_state",
            _required_text(self.expected_terminal_state, "expected_terminal_state"),
        )
        object.__setattr__(
            self,
            "side_effect_assertions",
            _string_tuple(self.side_effect_assertions, "side_effect_assertions"),
        )
        object.__setattr__(
            self,
            "cleanup_assertions",
            _string_tuple(self.cleanup_assertions, "cleanup_assertions"),
        )
        object.__setattr__(self, "artifacts", _string_tuple(self.artifacts, "artifacts"))
        deadline_seconds = _finite_number(
            self.deadline_seconds,
            "deadline_seconds",
            allow_zero=False,
        )
        earliest_start = _finite_number(
            self.earliest_start,
            "earliest_start",
            allow_zero=True,
        )
        preferred_window = _preferred_window(self.preferred_window)
        hard_deadline = _finite_number(
            self.hard_deadline,
            "hard_deadline",
            allow_zero=False,
        )
        if earliest_start > preferred_window[0]:
            raise ContractValidationError(
                "earliest_start must be less than or equal to preferred_window.start"
            )
        if hard_deadline < preferred_window[1]:
            raise ContractValidationError(
                "hard_deadline must be greater than or equal to preferred_window.end"
            )
        if earliest_start + deadline_seconds > hard_deadline:
            raise ContractValidationError(
                "earliest_start plus deadline_seconds must fit before hard_deadline"
            )
        resource_class = _identifier_tuple(self.resource_class, "resource_class")
        conflicts_with = _identifier_tuple(
            self.conflicts_with,
            "conflicts_with",
            allow_empty=True,
        )
        if self.scenario_id in conflicts_with:
            raise ContractValidationError("conflicts_with cannot reference scenario_id itself")
        object.__setattr__(self, "deadline_seconds", deadline_seconds)
        object.__setattr__(self, "earliest_start", earliest_start)
        object.__setattr__(self, "preferred_window", preferred_window)
        object.__setattr__(self, "hard_deadline", hard_deadline)
        object.__setattr__(self, "resource_class", resource_class)
        object.__setattr__(self, "conflicts_with", conflicts_with)

    @property
    def id(self) -> str:
        """Short alias useful to schedulers while JSON keeps ``scenario_id``."""

        return self.scenario_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCENARIO_CONTRACT_SCHEMA_VERSION,
            "scenario_id": self.scenario_id,
            "domain": self.domain,
            "mandatory": self.mandatory,
            "role": self.role,
            "preconditions": list(self.preconditions),
            "steps": list(self.steps),
            "expected_terminal_state": self.expected_terminal_state,
            "side_effect_assertions": list(self.side_effect_assertions),
            "cleanup_assertions": list(self.cleanup_assertions),
            "artifacts": list(self.artifacts),
            "deadline_seconds": self.deadline_seconds,
            "earliest_start": self.earliest_start,
            "preferred_window": list(self.preferred_window),
            "hard_deadline": self.hard_deadline,
            "resource_class": list(self.resource_class),
            "conflicts_with": list(self.conflicts_with),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ScenarioContract":
        if not isinstance(payload, Mapping):
            raise ContractValidationError("scenario contract must be an object")
        expected = {
            "schema_version",
            "scenario_id",
            "domain",
            "mandatory",
            "role",
            "preconditions",
            "steps",
            "expected_terminal_state",
            "side_effect_assertions",
            "cleanup_assertions",
            "artifacts",
            "deadline_seconds",
            "earliest_start",
            "preferred_window",
            "hard_deadline",
            "resource_class",
            "conflicts_with",
        }
        missing = sorted(expected - set(payload))
        unexpected = sorted(set(payload) - expected)
        if missing or unexpected:
            raise ContractValidationError(
                f"scenario contract shape mismatch: missing={missing}, unexpected={unexpected}"
            )
        if payload.get("schema_version") != SCENARIO_CONTRACT_SCHEMA_VERSION:
            raise ContractValidationError(
                f"unsupported scenario contract schema_version: {payload.get('schema_version')!r}"
            )
        return cls(
            scenario_id=payload["scenario_id"],
            domain=payload["domain"],
            mandatory=payload["mandatory"],
            role=payload["role"],
            preconditions=payload["preconditions"],
            steps=payload["steps"],
            expected_terminal_state=payload["expected_terminal_state"],
            side_effect_assertions=payload["side_effect_assertions"],
            cleanup_assertions=payload["cleanup_assertions"],
            artifacts=payload["artifacts"],
            deadline_seconds=payload["deadline_seconds"],
            earliest_start=payload["earliest_start"],
            preferred_window=payload["preferred_window"],
            hard_deadline=payload["hard_deadline"],
            resource_class=payload["resource_class"],
            conflicts_with=payload["conflicts_with"],
        )

    @classmethod
    def from_coverage_contract(
        cls,
        scenario_id: str,
        coverage_contract: object,
        *,
        role: str,
        preconditions: Iterable[str],
        steps: Iterable[str],
        expected_terminal_state: str,
        cleanup_assertions: Iterable[str],
        artifacts: Iterable[str],
        deadline_seconds: float,
        earliest_start: float,
        preferred_window: tuple[float, float],
        hard_deadline: float,
        resource_class: Iterable[str],
        conflicts_with: Iterable[str],
        mandatory: bool = True,
    ) -> "ScenarioContract":
        """Adapt ``operation_coverage.CampaignScenarioContract`` without coupling.

        The existing coverage contract remains the authority for its category
        and required evidence.  Execution details that it does not contain stay
        explicit at the call site instead of being silently invented.
        """

        try:
            domain = getattr(coverage_contract, "category")
            evidence = getattr(coverage_contract, "required_evidence")
        except Exception as exc:  # pragma: no cover - defensive protocol guard
            raise ContractValidationError(
                "coverage_contract must expose category and required_evidence"
            ) from exc
        return cls(
            scenario_id=scenario_id,
            domain=domain,
            mandatory=mandatory,
            role=role,
            preconditions=tuple(preconditions),
            steps=tuple(steps),
            expected_terminal_state=expected_terminal_state,
            side_effect_assertions=tuple(sorted(evidence)),
            cleanup_assertions=tuple(cleanup_assertions),
            artifacts=tuple(artifacts),
            deadline_seconds=deadline_seconds,
            earliest_start=earliest_start,
            preferred_window=preferred_window,
            hard_deadline=hard_deadline,
            resource_class=tuple(resource_class),
            conflicts_with=tuple(conflicts_with),
        )


@dataclass(frozen=True)
class ScenarioResult:
    """Machine-readable result whose PASS state requires positive evidence."""

    scenario_id: str
    status: FormalResultStatus
    terminal_state: str
    elapsed_seconds: float
    side_effect_assertions: Mapping[str, bool]
    cleanup_assertions: Mapping[str, bool]
    artifact_ids: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", _identifier(self.scenario_id, "scenario_id"))
        try:
            status = FormalResultStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ContractValidationError(f"unknown formal result status: {self.status!r}") from exc
        object.__setattr__(self, "status", status)
        terminal = self.terminal_state.strip() if isinstance(self.terminal_state, str) else ""
        object.__setattr__(self, "terminal_state", terminal)
        if (
            isinstance(self.elapsed_seconds, bool)
            or not isinstance(self.elapsed_seconds, (int, float))
            or not math.isfinite(float(self.elapsed_seconds))
            or float(self.elapsed_seconds) < 0
        ):
            raise ContractValidationError("elapsed_seconds must be a finite non-negative number")
        object.__setattr__(self, "elapsed_seconds", float(self.elapsed_seconds))
        object.__setattr__(
            self,
            "side_effect_assertions",
            _assertion_map(self.side_effect_assertions, "side_effect_assertions"),
        )
        object.__setattr__(
            self,
            "cleanup_assertions",
            _assertion_map(self.cleanup_assertions, "cleanup_assertions"),
        )
        object.__setattr__(
            self,
            "artifact_ids",
            _string_tuple(self.artifact_ids, "artifact_ids", allow_empty=True),
        )
        object.__setattr__(
            self,
            "diagnostics",
            _string_tuple(self.diagnostics, "diagnostics", allow_empty=True),
        )
        if status.is_pass:
            if not terminal:
                raise ContractValidationError("PASS requires a terminal_state")
            if not self.side_effect_assertions or not all(self.side_effect_assertions.values()):
                raise ContractValidationError("PASS requires non-empty successful side-effect assertions")
            if not self.cleanup_assertions or not all(self.cleanup_assertions.values()):
                raise ContractValidationError("PASS requires non-empty successful cleanup assertions")
            if not self.artifact_ids:
                raise ContractValidationError("PASS requires at least one artifact")
            if self.diagnostics:
                raise ContractValidationError("PASS cannot contain failure diagnostics")
        elif not self.diagnostics:
            raise ContractValidationError(f"{status.value} requires diagnostics")

    def contract_errors(self, contract: ScenarioContract) -> tuple[str, ...]:
        errors: list[str] = []
        if self.scenario_id != contract.scenario_id:
            errors.append("scenario_id_mismatch")
        if self.terminal_state != contract.expected_terminal_state:
            errors.append("terminal_state_mismatch")
        if self.elapsed_seconds > contract.deadline_seconds:
            errors.append("deadline_exceeded")
        missing_side_effects = sorted(
            name
            for name in contract.side_effect_assertions
            if self.side_effect_assertions.get(name) is not True
        )
        if missing_side_effects:
            errors.append(f"side_effect_assertions_missing_or_failed:{','.join(missing_side_effects)}")
        missing_cleanup = sorted(
            name
            for name in contract.cleanup_assertions
            if self.cleanup_assertions.get(name) is not True
        )
        if missing_cleanup:
            errors.append(f"cleanup_assertions_missing_or_failed:{','.join(missing_cleanup)}")
        missing_artifacts = sorted(set(contract.artifacts) - set(self.artifact_ids))
        if missing_artifacts:
            errors.append(f"artifacts_missing:{','.join(missing_artifacts)}")
        return tuple(errors)

    def is_contract_pass(self, contract: ScenarioContract) -> bool:
        return self.status.is_pass and not self.contract_errors(contract)

    def to_dict(self, contract: ScenarioContract | None = None) -> dict[str, Any]:
        contract_errors = list(self.contract_errors(contract)) if contract else []
        return {
            "schema_version": SCENARIO_RESULT_SCHEMA_VERSION,
            "scenario_id": self.scenario_id,
            "status": self.status.value,
            "terminal_state": self.terminal_state,
            "elapsed_seconds": self.elapsed_seconds,
            "side_effect_assertions": dict(self.side_effect_assertions),
            "cleanup_assertions": dict(self.cleanup_assertions),
            "artifact_ids": list(self.artifact_ids),
            "diagnostics": list(self.diagnostics),
            "contract_errors": contract_errors,
            "contract_pass": self.status.is_pass and not contract_errors if contract else None,
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        contract: ScenarioContract | None = None,
    ) -> "ScenarioResult":
        """Rebuild a result and verify all persisted derived contract fields."""

        if not isinstance(payload, Mapping):
            raise ContractValidationError("scenario result must be an object")
        expected = {
            "schema_version",
            "scenario_id",
            "status",
            "terminal_state",
            "elapsed_seconds",
            "side_effect_assertions",
            "cleanup_assertions",
            "artifact_ids",
            "diagnostics",
            "contract_errors",
            "contract_pass",
        }
        missing = sorted(expected - set(payload))
        unexpected = sorted(set(payload) - expected)
        if missing or unexpected:
            raise ContractValidationError(
                f"scenario result shape mismatch: missing={missing}, unexpected={unexpected}"
            )
        if payload.get("schema_version") != SCENARIO_RESULT_SCHEMA_VERSION:
            raise ContractValidationError(
                f"unsupported scenario result schema_version: {payload.get('schema_version')!r}"
            )
        result = cls(
            scenario_id=payload["scenario_id"],
            status=payload["status"],
            terminal_state=payload["terminal_state"],
            elapsed_seconds=payload["elapsed_seconds"],
            side_effect_assertions=payload["side_effect_assertions"],
            cleanup_assertions=payload["cleanup_assertions"],
            artifact_ids=payload["artifact_ids"],
            diagnostics=payload["diagnostics"],
        )
        expected_errors = list(result.contract_errors(contract)) if contract else []
        expected_pass = result.status.is_pass and not expected_errors if contract else None
        if payload.get("contract_errors") != expected_errors:
            raise ContractValidationError("persisted contract_errors do not match recomputation")
        if payload.get("contract_pass") is not expected_pass:
            raise ContractValidationError("persisted contract_pass does not match recomputation")
        return result


_ROLLUP_PRECEDENCE = (
    FormalResultStatus.INVALIDATED,
    FormalResultStatus.INTERRUPTED,
    FormalResultStatus.FAIL_HARNESS,
    FormalResultStatus.FAIL_INFRA,
    FormalResultStatus.FAIL_EXTERNAL,
    FormalResultStatus.FAIL_PRODUCT,
    FormalResultStatus.BLOCKED,
)


def contract_set_errors(
    contracts: Mapping[str, ScenarioContract],
) -> tuple[str, ...]:
    """Validate mapping identity and scheduling conflict references as one set."""

    if not isinstance(contracts, Mapping) or not contracts:
        return ("contracts_empty_or_invalid",)
    errors: list[str] = []
    known_ids = set(contracts)
    for key, contract in contracts.items():
        if not isinstance(contract, ScenarioContract):
            errors.append(f"contract_type_invalid:{key}")
            continue
        if key != contract.scenario_id:
            errors.append(f"contract_mapping_key_mismatch:{key}")
        unknown_conflicts = sorted(set(contract.conflicts_with) - known_ids)
        if unknown_conflicts:
            errors.append(
                f"conflicts_reference_unknown_scenarios:{contract.scenario_id}:"
                f"{','.join(unknown_conflicts)}"
            )
    return tuple(errors)


def rollup_formal_status(
    results: Mapping[str, ScenarioResult],
    mandatory_contracts: Mapping[str, ScenarioContract],
) -> FormalResultStatus:
    """Return PASS only when every mandatory result satisfies its contract."""

    if contract_set_errors(mandatory_contracts):
        return FormalResultStatus.FAIL_HARNESS
    if not isinstance(results, Mapping):
        return FormalResultStatus.FAIL_HARNESS
    if any(
        not isinstance(result, ScenarioResult) or key != result.scenario_id
        for key, result in results.items()
    ):
        return FormalResultStatus.FAIL_HARNESS
    if set(results) - set(mandatory_contracts):
        return FormalResultStatus.FAIL_HARNESS
    mandatory = {
        scenario_id
        for scenario_id, contract in mandatory_contracts.items()
        if contract.mandatory
    }
    if not mandatory or not results or mandatory - set(results):
        return FormalResultStatus.FAIL_HARNESS
    if any(
        result.status is FormalResultStatus.PASS
        and not result.is_contract_pass(mandatory_contracts[scenario_id])
        for scenario_id, result in results.items()
    ):
        return FormalResultStatus.FAIL_HARNESS
    statuses = {results[scenario_id].status for scenario_id in mandatory}
    if statuses == {FormalResultStatus.PASS}:
        return FormalResultStatus.PASS
    return next(status for status in _ROLLUP_PRECEDENCE if status in statuses)


def build_formal_rollup(
    results: Mapping[str, ScenarioResult],
    contracts: Mapping[str, ScenarioContract],
) -> dict[str, Any]:
    """Return a self-describing rollup suitable for the campaign artifact index."""

    errors = list(contract_set_errors(contracts))
    if not isinstance(results, Mapping):
        results = {}
        errors.append("results_not_mapping")
    result_mapping_errors = [
        f"result_mapping_invalid:{key}"
        for key, result in results.items()
        if not isinstance(result, ScenarioResult) or key != result.scenario_id
    ]
    errors.extend(result_mapping_errors)
    unknown_results = sorted(set(results) - set(contracts))
    if unknown_results:
        errors.append(f"results_reference_unknown_contracts:{','.join(unknown_results)}")
    mandatory_ids = sorted(
        scenario_id
        for scenario_id, contract in contracts.items()
        if isinstance(contract, ScenarioContract) and contract.mandatory
    )
    missing_mandatory = sorted(set(mandatory_ids) - set(results))
    if missing_mandatory:
        errors.append(f"mandatory_results_missing:{','.join(missing_mandatory)}")
    serialized_results: dict[str, Any] = {}
    contract_pass_count = 0
    for scenario_id, result in sorted(results.items()):
        contract = contracts.get(scenario_id)
        if not isinstance(result, ScenarioResult) or not isinstance(contract, ScenarioContract):
            continue
        serialized = result.to_dict(contract)
        serialized_results[scenario_id] = serialized
        if serialized["contract_pass"] is True:
            contract_pass_count += 1
    status = rollup_formal_status(results, contracts)
    if status is FormalResultStatus.FAIL_HARNESS and not errors:
        invalid_passes = sorted(
            scenario_id
            for scenario_id, result in results.items()
            if scenario_id in contracts
            and isinstance(result, ScenarioResult)
            and result.status is FormalResultStatus.PASS
            and not result.is_contract_pass(contracts[scenario_id])
        )
        if invalid_passes:
            errors.append(f"passing_contract_mismatch:{','.join(invalid_passes)}")
    mandatory_pass_count = sum(
        isinstance(results.get(scenario_id), ScenarioResult)
        and results[scenario_id].is_contract_pass(contracts[scenario_id])
        for scenario_id in mandatory_ids
    )
    return {
        "schema_version": SCENARIO_ROLLUP_SCHEMA_VERSION,
        "status": status.value,
        "formal_pass": status is FormalResultStatus.PASS and not errors,
        "contract_count": len(contracts),
        "result_count": len(results),
        "mandatory_count": len(mandatory_ids),
        "mandatory_pass_count": mandatory_pass_count,
        "contract_pass_count": contract_pass_count,
        "mandatory_scenario_ids": mandatory_ids,
        "missing_mandatory_scenario_ids": missing_mandatory,
        "results": serialized_results,
        "errors": errors,
    }
