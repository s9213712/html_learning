"""Formal scenario-to-native-runner binding gate.

The reviewed scenario catalogue in :mod:`operation_coverage` says *what* the
formal campaign must prove.  This module is the fail-closed bridge to *how* it
is proved.  It deliberately does not guess that a successful process exit, a
truthy JSON field, or an HTTP acceptance response proves domain behaviour.

The module is independent from the campaign runner so the runner can import
and wire it only after every native adapter and validator really exists.
Until then, :func:`build_and_validate_formal_scenario_bindings` returns all 13
serialisable v2 contracts together with ``FAIL_HARNESS`` and exact missing
registration IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
import tarfile
import time
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from scripts.testing.campaign_artifacts import (
    ARTIFACT_RECORD_SCHEMA_VERSION,
    SECRET_SCAN_SCHEMA_VERSION,
    ArtifactSpec,
    ArtifactType,
    validate_artifact,
)
from scripts.testing.campaign_contract import (
    FormalResultStatus,
    ScenarioContract,
)
from scripts.testing.operation_coverage import (
    CAMPAIGN_SCENARIO_CONTRACTS,
    CampaignScenarioContract,
)


FORMAL_BINDING_SCHEMA_VERSION = "hackme.campaign.formal-scenario-binding/v1"
FORMAL_BINDING_GATE_SCHEMA_VERSION = "hackme.campaign.formal-scenario-binding-gate/v2"
RUNTIME_RECEIPT_SCHEMA_VERSION = "hackme.campaign.native-scenario-receipt/v2"
NATIVE_RUNNER_RESULT_SCHEMA_VERSION = "hackme.campaign.native-runner-result/v1"
NATIVE_EVIDENCE_MANIFEST_SCHEMA_VERSION = "hackme.campaign.native-evidence-manifest/v1"
NATIVE_EVIDENCE_SUMMARY_SCHEMA_VERSION = "hackme.campaign.native-evidence-summary/v1"
NATIVE_ARTIFACT_BUNDLE_SCHEMA_VERSION = "hackme.campaign.native-artifact-bundle/v2"
NATIVE_ARTIFACT_ARCHIVE_SCHEMA_VERSION = "hackme.campaign.native-artifact-archive/v1"
NATIVE_RUNTIME_PIPELINE_SCHEMA_VERSION = "hackme.campaign.native-runtime-pipeline/v2"
_FORMAL_DURATION_SECONDS = 86_400.0
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,159}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UUIDISH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_IMPLEMENTATION_REF = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$"
)
_SCENARIO_WALL_MONOTONIC_TOLERANCE_SECONDS = 5.0
_SCENARIO_AUTHORITY_FIELDS = frozenset({
    "qualification_campaign_uuid",
    "campaign_uuid",
    "campaign_attempt_uuid",
    "scenario_attempt_uuid",
    "native_invocation_id",
    "commit",
    "source_digest",
    "protected_source_digest",
    "started_at",
    "finished_at",
    "started_monotonic_ns",
    "finished_monotonic_ns",
})
_SCENARIO_AUTHORITY_IDENTITY_FIELDS = frozenset({
    "qualification_campaign_uuid",
    "campaign_uuid",
    "campaign_attempt_uuid",
    "scenario_attempt_uuid",
    "native_invocation_id",
    "commit",
    "source_digest",
    "protected_source_digest",
})
_BUNDLE_REFERENCE_FIELDS = frozenset({
    "artifact_id",
    "content_schema_version",
    "path",
    "sha256",
    "size_bytes",
    "manifest_sha256",
    "member_inventory_sha256",
    "member_count",
    "artifact_archive_id",
    "artifact_archive_sha256",
    "artifact_archive_size_bytes",
})
_ARCHIVE_REFERENCE_FIELDS = frozenset({
    "artifact_id",
    "content_schema_version",
    "path",
    "sha256",
    "size_bytes",
    "media_type",
})
_MEMBER_INVENTORY_FIELDS = frozenset({
    "artifact_id",
    "member_path",
    "sha256",
    "size_bytes",
    "artifact_type",
})
_ARTIFACT_RECORD_FIELDS = frozenset({
    "schema_version",
    "artifact_id",
    "scenario_id",
    "path",
    "created_at",
    "type",
    "mandatory",
    "scenario_link_valid",
    "within_artifact_root",
    "exists",
    "regular_file",
    "size",
    "minimum_size_bytes",
    "nonzero",
    "validation_snapshot_stable",
    "sha256",
    "expected_sha256",
    "sha256_verified",
    "format_validation",
    "secret_scan",
    "validated",
    "errors",
})
_EVIDENCE_RESULT_FIELDS = frozenset({
    "evidence_id",
    "adapter_id",
    "validated",
    "native_observation_ids",
    "failure_class",
    "diagnostics",
})
_VALIDATOR_RESULT_FIELDS = frozenset({
    "validator_id",
    "kind",
    "passed",
    "native_observation_ids",
    "failure_class",
    "diagnostics",
})


# These blockers are an audit of the pre-existing operational campaign
# methods, not waivers.  Keeping them beside the reviewed binding manifest
# makes the gate explain *why* a callable has not been registered instead of
# reducing the failure to a very long list of opaque IDs.  Every reviewed
# scenario stays mandatory and every tuple must become empty before a formal
# binding may be considered complete.
AUDITED_NATIVE_BINDING_BLOCKERS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "media_long_hls_share": (),
    "cloud_drive_share_stream": (),
    "bt_download_stream_restart": (),
    "ai_agent_positive_operations": (),
    "comfyui_real_workflows": (),
    "trading_background_custom_workflow": (),
    "pointschain_hft_invariants": (),
    "wallet_incident_governance": (),
    "backup_restore_restart": (),
    "server_emergency_incident": (),
    "media_proxy_cross_browser": (),
    "community_governance_operations": (),
    "final_ui_mobile_prelaunch": (),
})


class BindingValidationError(ValueError):
    """Raised when a binding design cannot safely become a v2 contract."""


class ValidatorKind(str, Enum):
    TERMINAL = "terminal"
    CLEANUP = "cleanup"
    ARTIFACT = "artifact"


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BindingValidationError(f"{label} must be a non-empty string")
    return value.strip()


def _identifier(value: object, label: str) -> str:
    text = _required_text(value, label)
    if not _IDENTIFIER.fullmatch(text):
        raise BindingValidationError(f"{label} is not a valid identifier: {text!r}")
    return text


def _implementation_ref(value: object) -> str:
    text = _required_text(value, "implementation_ref")
    if not _IMPLEMENTATION_REF.fullmatch(text):
        raise BindingValidationError(
            "implementation_ref must be an explicit module.path:callable_name"
        )
    return text


def _validated_handler(
    handler: object,
    implementation_ref: str,
    label: str,
) -> Callable[..., object]:
    """Require the callable to match its declared module and native name.

    A handwritten registration previously could name an arbitrary production
    implementation while pointing at an unrelated lambda.  That was enough
    to make the structural gate green.  Bound methods and normal functions
    expose the two attributes below, so fail closed for partials, callable
    objects, aliases, and forged references until a deliberate adapter exists.
    """

    if not callable(handler):
        raise BindingValidationError(f"{label} handler must be callable")
    module_name, callable_name = implementation_ref.split(":", 1)
    actual_module = str(getattr(handler, "__module__", "") or "")
    actual_name = str(getattr(handler, "__name__", "") or "")
    if actual_module != module_name or actual_name != callable_name:
        raise BindingValidationError(
            f"{label} implementation_ref does not match handler provenance: "
            f"declared={implementation_ref!r}, actual={actual_module}:{actual_name}"
        )
    return handler


def _exact_fraction(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 < float(value) < 1.0
    ):
        raise BindingValidationError(f"{label} must be a finite fraction between 0 and 1")
    return float(value)


def _string_tuple(values: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise BindingValidationError(f"{label} must be a sequence")
    result = tuple(_required_text(value, f"{label} item") for value in values)
    if not allow_empty and not result:
        raise BindingValidationError(f"{label} must not be empty")
    if len(result) != len(set(result)):
        raise BindingValidationError(f"{label} contains duplicates")
    return result


@dataclass(frozen=True)
class FormalScenarioBinding:
    """Reviewed identity plus the exact native callable IDs it requires."""

    scenario_id: str
    category: str
    scheduled_fraction: float
    evidence_adapter_ids: Mapping[str, str]
    runner_id: str
    terminal_validator_ids: tuple[str, ...]
    cleanup_validator_ids: tuple[str, ...]
    artifact_validator_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        scenario_id = _identifier(self.scenario_id, "scenario_id")
        category = _identifier(self.category, "category")
        fraction = _exact_fraction(self.scheduled_fraction, "scheduled_fraction")
        if not isinstance(self.evidence_adapter_ids, Mapping) or not self.evidence_adapter_ids:
            raise BindingValidationError("evidence_adapter_ids must not be empty")
        evidence: dict[str, str] = {}
        for evidence_id, adapter_id in self.evidence_adapter_ids.items():
            evidence[_identifier(evidence_id, "evidence_id")] = _identifier(
                adapter_id, "adapter_id"
            )
        if len(set(evidence.values())) != len(evidence):
            raise BindingValidationError("every evidence item needs a unique native adapter ID")
        object.__setattr__(self, "scenario_id", scenario_id)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "scheduled_fraction", fraction)
        object.__setattr__(self, "evidence_adapter_ids", MappingProxyType(evidence))
        object.__setattr__(self, "runner_id", _identifier(self.runner_id, "runner_id"))
        for field_name in (
            "terminal_validator_ids",
            "cleanup_validator_ids",
            "artifact_validator_ids",
        ):
            values = _string_tuple(getattr(self, field_name), field_name)
            values = tuple(_identifier(value, field_name) for value in values)
            object.__setattr__(self, field_name, values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FORMAL_BINDING_SCHEMA_VERSION,
            "scenario_id": self.scenario_id,
            "category": self.category,
            "scheduled_fraction": self.scheduled_fraction,
            "evidence_adapter_ids": dict(sorted(self.evidence_adapter_ids.items())),
            "runner_id": self.runner_id,
            "terminal_validator_ids": list(self.terminal_validator_ids),
            "cleanup_validator_ids": list(self.cleanup_validator_ids),
            "artifact_validator_ids": list(self.artifact_validator_ids),
        }


@dataclass(frozen=True)
class NativeEvidenceAdapterRegistration:
    adapter_id: str
    scenario_id: str
    evidence_id: str
    implementation_ref: str
    handler: Callable[..., object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter_id", _identifier(self.adapter_id, "adapter_id"))
        object.__setattr__(self, "scenario_id", _identifier(self.scenario_id, "scenario_id"))
        object.__setattr__(self, "evidence_id", _identifier(self.evidence_id, "evidence_id"))
        implementation_ref = _implementation_ref(self.implementation_ref)
        object.__setattr__(self, "implementation_ref", implementation_ref)
        _validated_handler(self.handler, implementation_ref, "native evidence adapter")


@dataclass(frozen=True)
class ScenarioRunnerRegistration:
    runner_id: str
    scenario_id: str
    implementation_ref: str
    handler: Callable[..., object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "runner_id", _identifier(self.runner_id, "runner_id"))
        object.__setattr__(self, "scenario_id", _identifier(self.scenario_id, "scenario_id"))
        implementation_ref = _implementation_ref(self.implementation_ref)
        object.__setattr__(self, "implementation_ref", implementation_ref)
        _validated_handler(self.handler, implementation_ref, "scenario runner")


@dataclass(frozen=True)
class ScenarioValidatorRegistration:
    validator_id: str
    scenario_id: str
    kind: ValidatorKind
    implementation_ref: str
    handler: Callable[..., object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "validator_id", _identifier(self.validator_id, "validator_id"))
        object.__setattr__(self, "scenario_id", _identifier(self.scenario_id, "scenario_id"))
        try:
            kind = ValidatorKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise BindingValidationError(f"unknown validator kind: {self.kind!r}") from exc
        object.__setattr__(self, "kind", kind)
        implementation_ref = _implementation_ref(self.implementation_ref)
        object.__setattr__(self, "implementation_ref", implementation_ref)
        _validated_handler(self.handler, implementation_ref, "scenario validator")


@dataclass(frozen=True)
class FormalBindingGateResult:
    status: FormalResultStatus
    contracts: Mapping[str, ScenarioContract]
    bindings: Mapping[str, FormalScenarioBinding]
    registration_coverage: Mapping[str, Mapping[str, Any]]
    binding_blockers: Mapping[str, tuple[str, ...]]
    runtime_execution_pipeline_verified: bool
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.status is FormalResultStatus.PASS and not self.errors

    def to_dict(self) -> dict[str, Any]:
        fully_bound = sorted(
            scenario_id
            for scenario_id, coverage in self.registration_coverage.items()
            if coverage.get("fully_bound") is True
        )
        return {
            "schema_version": FORMAL_BINDING_GATE_SCHEMA_VERSION,
            "status": self.status.value,
            "gate_pass": self.passed,
            "formal_campaign_pass": False,
            "runtime_execution_pipeline_verified": self.runtime_execution_pipeline_verified,
            "reviewed_scenario_count": len(_CANONICAL_REVIEWED_SIGNATURES),
            "contract_count": len(self.contracts),
            "binding_count": len(self.bindings),
            "required_evidence_count": sum(
                len(binding.evidence_adapter_ids) for binding in self.bindings.values()
            ),
            "registered_runner_count": sum(
                coverage.get("runner_registered") is True
                for coverage in self.registration_coverage.values()
            ),
            "registered_evidence_adapter_count": sum(
                len(coverage.get("registered_evidence_adapter_ids") or [])
                for coverage in self.registration_coverage.values()
            ),
            "registered_validator_count": sum(
                len(coverage.get("registered_terminal_validator_ids") or [])
                + len(coverage.get("registered_cleanup_validator_ids") or [])
                + len(coverage.get("registered_artifact_validator_ids") or [])
                for coverage in self.registration_coverage.values()
            ),
            "fully_bound_scenario_count": len(fully_bound),
            "fully_bound_scenario_ids": fully_bound,
            "contracts": {
                scenario_id: contract.to_dict()
                for scenario_id, contract in sorted(self.contracts.items())
            },
            "bindings": {
                scenario_id: binding.to_dict()
                for scenario_id, binding in sorted(self.bindings.items())
                if isinstance(binding, FormalScenarioBinding)
            },
            "registration_coverage": {
                scenario_id: dict(coverage)
                for scenario_id, coverage in sorted(self.registration_coverage.items())
            },
            "binding_blockers": {
                scenario_id: list(blockers)
                for scenario_id, blockers in sorted(self.binding_blockers.items())
            },
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class RuntimeReceiptValidation:
    status: FormalResultStatus
    valid: bool
    contract_pass: bool
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RUNTIME_RECEIPT_SCHEMA_VERSION,
            "status": self.status.value,
            "valid": self.valid,
            "contract_pass": self.contract_pass,
            "errors": list(self.errors),
        }


def _capture_reviewed_signatures(
    reviewed: Mapping[str, CampaignScenarioContract],
) -> Mapping[str, tuple[str, float, tuple[str, ...], str]]:
    return MappingProxyType(
        {
            scenario_id: (
                contract.category,
                float(contract.scheduled_fraction),
                tuple(sorted(contract.required_evidence)),
                contract.resource_class,
            )
            for scenario_id, contract in reviewed.items()
        }
    )


_CANONICAL_REVIEWED_SIGNATURES = _capture_reviewed_signatures(
    CAMPAIGN_SCENARIO_CONTRACTS
)


def _adapter_id(scenario_id: str, evidence_id: str) -> str:
    return f"native.adapter.{scenario_id}.{evidence_id}"


def _binding_from_signature(
    scenario_id: str,
    signature: tuple[str, float, tuple[str, ...], str],
) -> FormalScenarioBinding:
    category, fraction, evidence_ids, _resource_class = signature
    return FormalScenarioBinding(
        scenario_id=scenario_id,
        category=category,
        scheduled_fraction=fraction,
        evidence_adapter_ids={
            evidence_id: _adapter_id(scenario_id, evidence_id)
            for evidence_id in evidence_ids
        },
        runner_id=f"native.runner.{scenario_id}",
        terminal_validator_ids=(f"native.validator.terminal.{scenario_id}",),
        cleanup_validator_ids=(f"native.validator.cleanup.{scenario_id}",),
        artifact_validator_ids=(f"native.validator.artifact.{scenario_id}",),
    )


# This immutable manifest gives every reviewed evidence item an explicit,
# non-alias native adapter ID.  IDs are registrations, not claims that the
# implementations already exist.
FORMAL_SCENARIO_BINDINGS: Mapping[str, FormalScenarioBinding] = MappingProxyType(
    {
        scenario_id: _binding_from_signature(scenario_id, signature)
        for scenario_id, signature in _CANONICAL_REVIEWED_SIGNATURES.items()
    }
)


def _reviewed_contract_errors(
    reviewed: object,
) -> list[str]:
    if not isinstance(reviewed, Mapping) or not reviewed:
        return ["reviewed_contracts_empty_or_invalid"]
    errors: list[str] = []
    expected_ids = set(_CANONICAL_REVIEWED_SIGNATURES)
    actual_ids = set(reviewed)
    missing = sorted(expected_ids - actual_ids)
    extra = sorted(actual_ids - expected_ids)
    if missing:
        errors.append(f"reviewed_scenario_ids_missing:{','.join(missing)}")
    if extra:
        errors.append(f"reviewed_scenario_ids_extra:{','.join(extra)}")
    for scenario_id in sorted(expected_ids & actual_ids):
        contract = reviewed[scenario_id]
        if not isinstance(contract, CampaignScenarioContract):
            errors.append(f"reviewed_contract_type_invalid:{scenario_id}")
            continue
        expected = _CANONICAL_REVIEWED_SIGNATURES[scenario_id]
        actual = (
            contract.category,
            contract.scheduled_fraction,
            tuple(sorted(contract.required_evidence)),
            contract.resource_class,
        )
        if actual[0] != expected[0]:
            errors.append(f"reviewed_category_mismatch:{scenario_id}")
        if actual[1] != expected[1]:
            errors.append(f"reviewed_scheduled_fraction_mismatch:{scenario_id}")
        if actual[2] != expected[2]:
            errors.append(f"reviewed_required_evidence_mismatch:{scenario_id}")
        if actual[3] != expected[3]:
            errors.append(f"reviewed_resource_class_mismatch:{scenario_id}")
    return errors


def _binding_manifest_errors(bindings: object) -> list[str]:
    if not isinstance(bindings, Mapping) or not bindings:
        return ["binding_manifest_empty_or_invalid"]
    errors: list[str] = []
    expected_ids = set(FORMAL_SCENARIO_BINDINGS)
    actual_ids = set(bindings)
    missing = sorted(expected_ids - actual_ids)
    extra = sorted(actual_ids - expected_ids)
    if missing:
        errors.append(f"binding_scenario_ids_missing:{','.join(missing)}")
    if extra:
        errors.append(f"binding_scenario_ids_extra:{','.join(extra)}")
    for scenario_id in sorted(expected_ids & actual_ids):
        binding = bindings[scenario_id]
        if not isinstance(binding, FormalScenarioBinding):
            errors.append(f"binding_type_invalid:{scenario_id}")
            continue
        expected = FORMAL_SCENARIO_BINDINGS[scenario_id]
        if scenario_id != binding.scenario_id:
            errors.append(f"binding_mapping_key_mismatch:{scenario_id}")
        if binding.category != expected.category:
            errors.append(f"binding_category_mismatch:{scenario_id}")
        if binding.scheduled_fraction != expected.scheduled_fraction:
            errors.append(f"binding_scheduled_fraction_mismatch:{scenario_id}")
        if dict(binding.evidence_adapter_ids) != dict(expected.evidence_adapter_ids):
            errors.append(f"binding_evidence_adapter_ids_mismatch:{scenario_id}")
        if binding.runner_id != expected.runner_id:
            errors.append(f"binding_runner_id_mismatch:{scenario_id}")
        for field_name in (
            "terminal_validator_ids",
            "cleanup_validator_ids",
            "artifact_validator_ids",
        ):
            if getattr(binding, field_name) != getattr(expected, field_name):
                errors.append(f"binding_{field_name}_mismatch:{scenario_id}")
    adapter_ids = [
        adapter_id
        for binding in bindings.values()
        if isinstance(binding, FormalScenarioBinding)
        for adapter_id in binding.evidence_adapter_ids.values()
    ]
    if len(adapter_ids) != len(set(adapter_ids)):
        errors.append("binding_adapter_ids_not_globally_unique")
    return errors


def _binding_blocker_errors(blockers: object) -> list[str]:
    """Validate the audited implementation gaps and make every gap blocking."""

    if not isinstance(blockers, Mapping):
        return ["native_binding_blockers_not_mapping"]
    expected_ids = set(_CANONICAL_REVIEWED_SIGNATURES)
    actual_ids = set(blockers)
    errors: list[str] = []
    missing = sorted(expected_ids - actual_ids)
    extra = sorted(actual_ids - expected_ids)
    if missing:
        errors.append(f"native_binding_blocker_scenarios_missing:{','.join(missing)}")
    if extra:
        errors.append(f"native_binding_blocker_scenarios_extra:{','.join(extra)}")
    for scenario_id in sorted(expected_ids & actual_ids):
        try:
            scenario_blockers = _string_tuple(
                blockers[scenario_id],
                f"native_binding_blockers:{scenario_id}",
                allow_empty=True,
            )
            scenario_blockers = tuple(
                _identifier(item, f"native_binding_blocker:{scenario_id}")
                for item in scenario_blockers
            )
        except BindingValidationError:
            errors.append(f"native_binding_blockers_invalid:{scenario_id}")
            continue
        if scenario_blockers:
            errors.append(
                f"native_binding_blockers_present:{scenario_id}:"
                f"{','.join(scenario_blockers)}"
            )
    return errors


def _expected_adapter_bindings(
    bindings: Mapping[str, FormalScenarioBinding],
) -> dict[str, tuple[str, str]]:
    return {
        adapter_id: (scenario_id, evidence_id)
        for scenario_id, binding in bindings.items()
        for evidence_id, adapter_id in binding.evidence_adapter_ids.items()
    }


def _expected_validator_bindings(
    bindings: Mapping[str, FormalScenarioBinding],
) -> dict[str, tuple[str, ValidatorKind]]:
    expected: dict[str, tuple[str, ValidatorKind]] = {}
    for scenario_id, binding in bindings.items():
        for kind, ids in (
            (ValidatorKind.TERMINAL, binding.terminal_validator_ids),
            (ValidatorKind.CLEANUP, binding.cleanup_validator_ids),
            (ValidatorKind.ARTIFACT, binding.artifact_validator_ids),
        ):
            expected.update({validator_id: (scenario_id, kind) for validator_id in ids})
    return expected


def _registry_shape_errors(
    label: str,
    registry: object,
    expected_ids: set[str],
) -> tuple[list[str], Mapping[str, object]]:
    if registry is None:
        registry = {}
    if not isinstance(registry, Mapping):
        return [f"{label}_registry_not_mapping"], {}
    errors: list[str] = []
    actual_ids = set(registry)
    missing = sorted(expected_ids - actual_ids)
    extra = sorted(actual_ids - expected_ids)
    if missing:
        errors.append(f"{label}_registrations_missing:{','.join(missing)}")
    if extra:
        errors.append(f"{label}_registrations_extra:{','.join(extra)}")
    return errors, registry


def _registration_matches_adapter(
    registration: object,
    *,
    adapter_id: str,
    scenario_id: str,
    evidence_id: str,
) -> bool:
    return bool(
        isinstance(registration, NativeEvidenceAdapterRegistration)
        and registration.adapter_id == adapter_id
        and registration.scenario_id == scenario_id
        and registration.evidence_id == evidence_id
    )


def _registration_matches_runner(
    registration: object,
    *,
    runner_id: str,
    scenario_id: str,
) -> bool:
    return bool(
        isinstance(registration, ScenarioRunnerRegistration)
        and registration.runner_id == runner_id
        and registration.scenario_id == scenario_id
    )


def _registration_matches_validator(
    registration: object,
    *,
    validator_id: str,
    scenario_id: str,
    kind: ValidatorKind,
) -> bool:
    return bool(
        isinstance(registration, ScenarioValidatorRegistration)
        and registration.validator_id == validator_id
        and registration.scenario_id == scenario_id
        and registration.kind is kind
    )


def scenario_registration_coverage(
    *,
    bindings: Mapping[str, FormalScenarioBinding] = FORMAL_SCENARIO_BINDINGS,
    adapter_registry: Mapping[str, NativeEvidenceAdapterRegistration] | None = None,
    runner_registry: Mapping[str, ScenarioRunnerRegistration] | None = None,
    validator_registry: Mapping[str, ScenarioValidatorRegistration] | None = None,
    binding_blockers: Mapping[str, tuple[str, ...]] = AUDITED_NATIVE_BINDING_BLOCKERS,
    runtime_execution_pipeline_verified: bool = False,
) -> Mapping[str, Mapping[str, Any]]:
    """Return per-scenario exact registration coverage without claiming PASS."""

    adapters = adapter_registry if isinstance(adapter_registry, Mapping) else {}
    runners = runner_registry if isinstance(runner_registry, Mapping) else {}
    validators = validator_registry if isinstance(validator_registry, Mapping) else {}
    blockers_by_scenario = binding_blockers if isinstance(binding_blockers, Mapping) else {}
    result: dict[str, Mapping[str, Any]] = {}
    for scenario_id, binding in sorted(bindings.items()):
        if not isinstance(binding, FormalScenarioBinding):
            continue
        runner_registered = _registration_matches_runner(
            runners.get(binding.runner_id),
            runner_id=binding.runner_id,
            scenario_id=scenario_id,
        )
        registered_evidence = sorted(
            adapter_id
            for evidence_id, adapter_id in binding.evidence_adapter_ids.items()
            if _registration_matches_adapter(
                adapters.get(adapter_id),
                adapter_id=adapter_id,
                scenario_id=scenario_id,
                evidence_id=evidence_id,
            )
        )
        validator_groups: dict[ValidatorKind, tuple[str, ...]] = {
            ValidatorKind.TERMINAL: binding.terminal_validator_ids,
            ValidatorKind.CLEANUP: binding.cleanup_validator_ids,
            ValidatorKind.ARTIFACT: binding.artifact_validator_ids,
        }
        registered_validators = {
            kind: sorted(
                validator_id
                for validator_id in ids
                if _registration_matches_validator(
                    validators.get(validator_id),
                    validator_id=validator_id,
                    scenario_id=scenario_id,
                    kind=kind,
                )
            )
            for kind, ids in validator_groups.items()
        }
        missing_evidence = sorted(
            set(binding.evidence_adapter_ids.values()) - set(registered_evidence)
        )
        missing_validators = {
            kind: sorted(set(ids) - set(registered_validators[kind]))
            for kind, ids in validator_groups.items()
        }
        audited_blockers = tuple(blockers_by_scenario.get(scenario_id) or ())
        registrations_complete = bool(
            runner_registered
            and not missing_evidence
            and all(not values for values in missing_validators.values())
        )
        result[scenario_id] = MappingProxyType({
            "runner_id": binding.runner_id,
            "runner_registered": runner_registered,
            "registered_evidence_adapter_ids": registered_evidence,
            "missing_evidence_adapter_ids": missing_evidence,
            "registered_terminal_validator_ids": registered_validators[ValidatorKind.TERMINAL],
            "missing_terminal_validator_ids": missing_validators[ValidatorKind.TERMINAL],
            "registered_cleanup_validator_ids": registered_validators[ValidatorKind.CLEANUP],
            "missing_cleanup_validator_ids": missing_validators[ValidatorKind.CLEANUP],
            "registered_artifact_validator_ids": registered_validators[ValidatorKind.ARTIFACT],
            "missing_artifact_validator_ids": missing_validators[ValidatorKind.ARTIFACT],
            "registrations_complete": registrations_complete,
            "audited_blockers": list(audited_blockers),
            "runtime_execution_pipeline_verified": runtime_execution_pipeline_verified is True,
            "fully_bound": (
                registrations_complete
                and not audited_blockers
                and runtime_execution_pipeline_verified is True
            ),
        })
    return MappingProxyType(result)


def validate_formal_scenario_bindings(
    *,
    reviewed_contracts: Mapping[str, CampaignScenarioContract] = CAMPAIGN_SCENARIO_CONTRACTS,
    bindings: Mapping[str, FormalScenarioBinding] = FORMAL_SCENARIO_BINDINGS,
    adapter_registry: Mapping[str, NativeEvidenceAdapterRegistration] | None = None,
    runner_registry: Mapping[str, ScenarioRunnerRegistration] | None = None,
    validator_registry: Mapping[str, ScenarioValidatorRegistration] | None = None,
    binding_blockers: Mapping[str, tuple[str, ...]] = AUDITED_NATIVE_BINDING_BLOCKERS,
    runtime_execution_pipeline_verified: bool = False,
) -> tuple[str, ...]:
    """Return exact fail-closed binding errors; an empty tuple is gate PASS."""

    errors = _reviewed_contract_errors(reviewed_contracts)
    errors.extend(_binding_manifest_errors(bindings))
    if errors:
        return tuple(sorted(set(errors)))
    errors.extend(_binding_blocker_errors(binding_blockers))
    if runtime_execution_pipeline_verified is not True:
        errors.append("native_runtime_execution_pipeline_not_verified")

    expected_adapters = _expected_adapter_bindings(bindings)
    shape_errors, adapters = _registry_shape_errors(
        "adapter", adapter_registry, set(expected_adapters)
    )
    errors.extend(shape_errors)
    for adapter_id in sorted(set(adapters) & set(expected_adapters)):
        registration = adapters[adapter_id]
        scenario_id, evidence_id = expected_adapters[adapter_id]
        if not isinstance(registration, NativeEvidenceAdapterRegistration):
            errors.append(f"adapter_registration_type_invalid:{adapter_id}")
            continue
        if registration.adapter_id != adapter_id:
            errors.append(f"adapter_registration_key_mismatch:{adapter_id}")
        if registration.scenario_id != scenario_id:
            errors.append(f"adapter_registration_scenario_mismatch:{adapter_id}")
        if registration.evidence_id != evidence_id:
            errors.append(f"adapter_registration_evidence_mismatch:{adapter_id}")

    expected_runners = {
        binding.runner_id: scenario_id for scenario_id, binding in bindings.items()
    }
    shape_errors, runners = _registry_shape_errors(
        "runner", runner_registry, set(expected_runners)
    )
    errors.extend(shape_errors)
    for runner_id in sorted(set(runners) & set(expected_runners)):
        registration = runners[runner_id]
        if not isinstance(registration, ScenarioRunnerRegistration):
            errors.append(f"runner_registration_type_invalid:{runner_id}")
            continue
        if registration.runner_id != runner_id:
            errors.append(f"runner_registration_key_mismatch:{runner_id}")
        if registration.scenario_id != expected_runners[runner_id]:
            errors.append(f"runner_registration_scenario_mismatch:{runner_id}")

    expected_validators = _expected_validator_bindings(bindings)
    shape_errors, validators = _registry_shape_errors(
        "validator", validator_registry, set(expected_validators)
    )
    errors.extend(shape_errors)
    for validator_id in sorted(set(validators) & set(expected_validators)):
        registration = validators[validator_id]
        scenario_id, kind = expected_validators[validator_id]
        if not isinstance(registration, ScenarioValidatorRegistration):
            errors.append(f"validator_registration_type_invalid:{validator_id}")
            continue
        if registration.validator_id != validator_id:
            errors.append(f"validator_registration_key_mismatch:{validator_id}")
        if registration.scenario_id != scenario_id:
            errors.append(f"validator_registration_scenario_mismatch:{validator_id}")
        if registration.kind is not kind:
            errors.append(f"validator_registration_kind_mismatch:{validator_id}")
    return tuple(sorted(set(errors)))


def build_formal_scenario_contracts(
    *,
    reviewed_contracts: Mapping[str, CampaignScenarioContract] = CAMPAIGN_SCENARIO_CONTRACTS,
    bindings: Mapping[str, FormalScenarioBinding] = FORMAL_SCENARIO_BINDINGS,
) -> Mapping[str, ScenarioContract]:
    """Build the 13 campaign-contract/v2 objects without claiming execution."""

    structural_errors = _reviewed_contract_errors(reviewed_contracts)
    structural_errors.extend(_binding_manifest_errors(bindings))
    if structural_errors:
        raise BindingValidationError(";".join(sorted(set(structural_errors))))
    contracts: dict[str, ScenarioContract] = {}
    for scenario_id, binding in bindings.items():
        reviewed = reviewed_contracts[scenario_id]
        scheduled_at = binding.scheduled_fraction * _FORMAL_DURATION_SECONDS
        preferred_end = min(_FORMAL_DURATION_SECONDS, scheduled_at + 900.0)
        contracts[scenario_id] = ScenarioContract.from_coverage_contract(
            scenario_id,
            reviewed,
            role="registered_native_scenario_runner",
            preconditions=(
                "source_frozen",
                "formal_dependencies_ready",
                "load_admission_open",
            ),
            steps=(
                binding.runner_id,
                *binding.terminal_validator_ids,
                *binding.cleanup_validator_ids,
                *binding.artifact_validator_ids,
            ),
            expected_terminal_state="success",
            cleanup_assertions=binding.cleanup_validator_ids,
            artifacts=(f"native.artifact.bundle.{scenario_id}",),
            deadline_seconds=7200.0,
            earliest_start=scheduled_at,
            preferred_window=(scheduled_at, preferred_end),
            hard_deadline=_FORMAL_DURATION_SECONDS,
            resource_class=(reviewed.resource_class,),
            conflicts_with=(),
        )
    return MappingProxyType(contracts)


def build_and_validate_formal_scenario_bindings(
    *,
    reviewed_contracts: Mapping[str, CampaignScenarioContract] = CAMPAIGN_SCENARIO_CONTRACTS,
    bindings: Mapping[str, FormalScenarioBinding] = FORMAL_SCENARIO_BINDINGS,
    adapter_registry: Mapping[str, NativeEvidenceAdapterRegistration] | None = None,
    runner_registry: Mapping[str, ScenarioRunnerRegistration] | None = None,
    validator_registry: Mapping[str, ScenarioValidatorRegistration] | None = None,
    binding_blockers: Mapping[str, tuple[str, ...]] = AUDITED_NATIVE_BINDING_BLOCKERS,
    runtime_execution_pipeline_verified: bool = False,
) -> FormalBindingGateResult:
    """Build contracts and validate registrations without weakening failures."""

    errors = validate_formal_scenario_bindings(
        reviewed_contracts=reviewed_contracts,
        bindings=bindings,
        adapter_registry=adapter_registry,
        runner_registry=runner_registry,
        validator_registry=validator_registry,
        binding_blockers=binding_blockers,
        runtime_execution_pipeline_verified=runtime_execution_pipeline_verified,
    )
    try:
        contracts = build_formal_scenario_contracts(
            reviewed_contracts=reviewed_contracts,
            bindings=bindings,
        )
    except BindingValidationError:
        contracts = MappingProxyType({})
    safe_bindings = {
        scenario_id: binding
        for scenario_id, binding in bindings.items()
        if isinstance(scenario_id, str) and isinstance(binding, FormalScenarioBinding)
    } if isinstance(bindings, Mapping) else {}
    safe_blockers = {
        scenario_id: tuple(blocker_values)
        for scenario_id, blocker_values in binding_blockers.items()
        if isinstance(scenario_id, str)
        and not isinstance(blocker_values, (str, bytes))
        and isinstance(blocker_values, Iterable)
        and all(isinstance(item, str) for item in blocker_values)
    } if isinstance(binding_blockers, Mapping) else {}
    return FormalBindingGateResult(
        status=(FormalResultStatus.PASS if not errors else FormalResultStatus.FAIL_HARNESS),
        contracts=contracts,
        bindings=MappingProxyType(safe_bindings),
        registration_coverage=scenario_registration_coverage(
            bindings=MappingProxyType(safe_bindings),
            adapter_registry=adapter_registry,
            runner_registry=runner_registry,
            validator_registry=validator_registry,
            binding_blockers=MappingProxyType(safe_blockers),
            runtime_execution_pipeline_verified=runtime_execution_pipeline_verified,
        ),
        binding_blockers=MappingProxyType(safe_blockers),
        runtime_execution_pipeline_verified=runtime_execution_pipeline_verified is True,
        errors=errors,
    )


_FORBIDDEN_SHORTCUT_KEYS = frozenset(
    {
        "ok",
        "raw_ok",
        "json_ok",
        "raw_json",
        "rc",
        "returncode",
        "http_status",
        "status_code",
        "skip",
        "skipped",
        "expected_gap",
    }
)
_FORBIDDEN_SHORTCUT_STRINGS = frozenset(
    {"ok", "rawok", "jsonok", "rc0", "http200", "http202", "skip", "skipped", "expectedgap"}
)


def _shortcut_errors(value: object, path: str = "receipt") -> list[str]:
    errors: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if normalized_key in _FORBIDDEN_SHORTCUT_KEYS:
                errors.append(f"shortcut_signal_forbidden:{path}.{key}")
            errors.extend(_shortcut_errors(child, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            errors.extend(_shortcut_errors(child, f"{path}[{index}]"))
    elif isinstance(value, str):
        normalized = re.sub(r"[^a-z0-9]", "", value.lower())
        if normalized in _FORBIDDEN_SHORTCUT_STRINGS:
            errors.append(f"shortcut_value_forbidden:{path}")
    return errors


def _exact_boolean_map(
    value: object,
    expected_ids: Iterable[str],
    label: str,
) -> tuple[list[str], dict[str, bool]]:
    if not isinstance(value, Mapping):
        return [f"{label}_not_mapping"], {}
    expected = set(expected_ids)
    actual = set(value)
    errors: list[str] = []
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        errors.append(f"{label}_missing:{','.join(missing)}")
    if extra:
        errors.append(f"{label}_extra:{','.join(extra)}")
    normalized: dict[str, bool] = {}
    for key in expected & actual:
        if type(value[key]) is not bool:
            errors.append(f"{label}_not_boolean:{key}")
        else:
            normalized[key] = value[key]
    return errors, normalized


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def scenario_member_inventory_sha256(value: object) -> str:
    """Return the canonical digest used to bind a bundle member inventory."""

    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _receipt_utc(value: object, label: str) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(timezone.utc)


def _precise_scenario_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical_path_text(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    candidate = Path(value)
    if not candidate.is_absolute() or value != str(candidate):
        return False
    if any(part in {"", ".", ".."} for part in candidate.parts[1:]):
        return False
    try:
        return candidate.resolve(strict=False) == candidate
    except OSError:
        return False


def scenario_authority_validation_errors(
    value: object,
    *,
    label: str = "runtime_receipt_authority",
) -> tuple[str, ...]:
    """Validate immutable campaign/scenario execution authority.

    This routine is intentionally filesystem independent.  Qualification
    capture and the gate consumer separately reopen the referenced files.
    """

    errors: list[str] = []
    if not isinstance(value, Mapping):
        return (f"{label}_not_mapping",)
    if set(value) != _SCENARIO_AUTHORITY_FIELDS:
        missing = sorted(_SCENARIO_AUTHORITY_FIELDS - set(value))
        extra = sorted(set(value) - _SCENARIO_AUTHORITY_FIELDS)
        if missing:
            errors.append(f"{label}_fields_missing:{','.join(missing)}")
        if extra:
            errors.append(f"{label}_fields_extra:{','.join(extra)}")
    identity = {
        field_name: value.get(field_name)
        for field_name in _SCENARIO_AUTHORITY_IDENTITY_FIELDS
    }
    errors.extend(
        scenario_authority_identity_validation_errors(identity, label=label)
    )
    started = _receipt_utc(value.get("started_at"), f"{label}.started_at")
    finished = _receipt_utc(value.get("finished_at"), f"{label}.finished_at")
    if started is None:
        errors.append(f"{label}_started_at_invalid")
    if finished is None:
        errors.append(f"{label}_finished_at_invalid")
    started_ns = value.get("started_monotonic_ns")
    finished_ns = value.get("finished_monotonic_ns")
    if (
        type(started_ns) is not int
        or type(finished_ns) is not int
        or started_ns <= 0
        or finished_ns <= started_ns
    ):
        errors.append(f"{label}_monotonic_boundary_invalid")
    if started is not None and finished is not None:
        wall_seconds = (finished - started).total_seconds()
        if wall_seconds <= 0:
            errors.append(f"{label}_wall_boundary_invalid")
        if (
            type(started_ns) is int
            and type(finished_ns) is int
            and finished_ns > started_ns
            and abs(
                wall_seconds - (finished_ns - started_ns) / 1_000_000_000.0
            ) > _SCENARIO_WALL_MONOTONIC_TOLERANCE_SECONDS
        ):
            errors.append(f"{label}_wall_monotonic_duration_mismatch")
    return tuple(sorted(set(errors)))


def scenario_authority_identity_validation_errors(
    value: object,
    *,
    label: str = "native_pipeline_authority_identity",
) -> tuple[str, ...]:
    """Validate only immutable caller-known authority; timing is pipeline-owned."""

    errors: list[str] = []
    if not isinstance(value, Mapping):
        return (f"{label}_not_mapping",)
    if set(value) != _SCENARIO_AUTHORITY_IDENTITY_FIELDS:
        missing = sorted(_SCENARIO_AUTHORITY_IDENTITY_FIELDS - set(value))
        extra = sorted(set(value) - _SCENARIO_AUTHORITY_IDENTITY_FIELDS)
        if missing:
            errors.append(f"{label}_fields_missing:{','.join(missing)}")
        if extra:
            errors.append(f"{label}_fields_extra:{','.join(extra)}")
    for field_name in (
        "qualification_campaign_uuid",
        "campaign_uuid",
        "campaign_attempt_uuid",
        "scenario_attempt_uuid",
        "native_invocation_id",
    ):
        if _UUIDISH.fullmatch(str(value.get(field_name) or "")) is None:
            errors.append(f"{label}_{field_name}_invalid")
    if _SHA40.fullmatch(str(value.get("commit") or "")) is None:
        errors.append(f"{label}_commit_invalid")
    for field_name in ("source_digest", "protected_source_digest"):
        if _SHA256.fullmatch(str(value.get(field_name) or "")) is None:
            errors.append(f"{label}_{field_name}_invalid")
    return tuple(sorted(set(errors)))


def _artifact_bundle_reference_errors(
    value: object,
    *,
    scenario_id: str,
    allow_empty_members: bool,
) -> tuple[str, ...]:
    label = "runtime_receipt_artifact_bundle"
    errors: list[str] = []
    if not isinstance(value, Mapping):
        return (f"{label}_not_mapping",)
    if set(value) != _BUNDLE_REFERENCE_FIELDS:
        missing = sorted(_BUNDLE_REFERENCE_FIELDS - set(value))
        extra = sorted(set(value) - _BUNDLE_REFERENCE_FIELDS)
        if missing:
            errors.append(f"{label}_fields_missing:{','.join(missing)}")
        if extra:
            errors.append(f"{label}_fields_extra:{','.join(extra)}")
    if value.get("artifact_id") != f"native.artifact.bundle.{scenario_id}":
        errors.append(f"{label}_id_mismatch")
    if value.get("content_schema_version") != NATIVE_ARTIFACT_BUNDLE_SCHEMA_VERSION:
        errors.append(f"{label}_schema_mismatch")
    if not _canonical_path_text(value.get("path")):
        errors.append(f"{label}_path_not_canonical")
    for field_name in (
        "sha256",
        "manifest_sha256",
        "member_inventory_sha256",
        "artifact_archive_sha256",
    ):
        if _SHA256.fullmatch(str(value.get(field_name) or "")) is None:
            errors.append(f"{label}_{field_name}_invalid")
    for field_name in ("size_bytes", "artifact_archive_size_bytes"):
        if type(value.get(field_name)) is not int or value.get(field_name, 0) <= 0:
            errors.append(f"{label}_{field_name}_invalid")
    member_count = value.get("member_count")
    if (
        type(member_count) is not int
        or member_count < 0
        or (not allow_empty_members and member_count == 0)
    ):
        errors.append(f"{label}_member_count_invalid")
    if value.get("artifact_archive_id") != f"native.artifact.archive.{scenario_id}":
        errors.append(f"{label}_archive_id_mismatch")
    return tuple(sorted(set(errors)))


def native_artifact_bundle_validation_errors(
    payload: object,
    binding: "FormalScenarioBinding",
    *,
    expected_authority: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Validate a v2 bundle and its complete archive member inventory."""

    label = "native_artifact_bundle"
    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return (f"{label}_not_mapping",)
    expected_fields = {
        "schema_version",
        "pipeline_schema_version",
        "scenario_id",
        "runner_id",
        "candidate_status",
        "authority",
        "artifact_records",
        "manifest_record",
        "artifact_archive",
        "member_inventory",
        "member_inventory_sha256",
        "evidence_adapter_results",
        "validator_results",
        "diagnostics",
    }
    missing = sorted(expected_fields - set(payload))
    extra = sorted(set(payload) - expected_fields)
    if missing:
        errors.append(f"{label}_fields_missing:{','.join(missing)}")
    if extra:
        errors.append(f"{label}_fields_extra:{','.join(extra)}")
    if payload.get("schema_version") != NATIVE_ARTIFACT_BUNDLE_SCHEMA_VERSION:
        errors.append(f"{label}_schema_mismatch")
    if payload.get("pipeline_schema_version") != NATIVE_RUNTIME_PIPELINE_SCHEMA_VERSION:
        errors.append(f"{label}_pipeline_schema_mismatch")
    if payload.get("scenario_id") != binding.scenario_id:
        errors.append(f"{label}_scenario_id_mismatch")
    if payload.get("runner_id") != binding.runner_id:
        errors.append(f"{label}_runner_id_mismatch")
    try:
        candidate_status = FormalResultStatus(payload.get("candidate_status"))
    except (TypeError, ValueError):
        candidate_status = FormalResultStatus.FAIL_HARNESS
        errors.append(f"{label}_candidate_status_invalid")
    authority = payload.get("authority")
    errors.extend(
        scenario_authority_validation_errors(
            authority,
            label=f"{label}_authority",
        )
    )
    if expected_authority is not None and dict(authority or {}) != dict(expected_authority):
        errors.append(f"{label}_authority_mismatch")

    archive = payload.get("artifact_archive")
    if not isinstance(archive, Mapping):
        errors.append(f"{label}_archive_not_mapping")
    else:
        if set(archive) != _ARCHIVE_REFERENCE_FIELDS:
            errors.append(f"{label}_archive_shape_mismatch")
        if archive.get("artifact_id") != f"native.artifact.archive.{binding.scenario_id}":
            errors.append(f"{label}_archive_id_mismatch")
        if archive.get("content_schema_version") != NATIVE_ARTIFACT_ARCHIVE_SCHEMA_VERSION:
            errors.append(f"{label}_archive_schema_mismatch")
        if archive.get("media_type") != "application/x-tar":
            errors.append(f"{label}_archive_media_type_mismatch")
        if not _canonical_path_text(archive.get("path")):
            errors.append(f"{label}_archive_path_not_canonical")
        if _SHA256.fullmatch(str(archive.get("sha256") or "")) is None:
            errors.append(f"{label}_archive_sha256_invalid")
        if type(archive.get("size_bytes")) is not int or archive.get("size_bytes", 0) <= 0:
            errors.append(f"{label}_archive_size_invalid")

    inventory = payload.get("member_inventory")
    normalized_inventory: dict[str, Mapping[str, Any]] = {}
    inventory_paths: set[str] = set()
    if not isinstance(inventory, list):
        errors.append(f"{label}_member_inventory_not_list")
        inventory = []
    for index, member in enumerate(inventory):
        if not isinstance(member, Mapping) or set(member) != _MEMBER_INVENTORY_FIELDS:
            errors.append(f"{label}_member_shape_mismatch:{index}")
            continue
        artifact_id = str(member.get("artifact_id") or "")
        member_path = str(member.get("member_path") or "")
        if _IDENTIFIER.fullmatch(artifact_id) is None or artifact_id in normalized_inventory:
            errors.append(f"{label}_member_artifact_id_invalid_or_duplicate:{index}")
            continue
        pure_path = PurePosixPath(member_path)
        if (
            not member_path
            or member_path.startswith("/")
            or "\\" in member_path
            or pure_path.as_posix() != member_path
            or any(part in {"", ".", ".."} for part in pure_path.parts)
            or member_path in inventory_paths
        ):
            errors.append(f"{label}_member_path_invalid_or_duplicate:{index}")
        inventory_paths.add(member_path)
        if _SHA256.fullmatch(str(member.get("sha256") or "")) is None:
            errors.append(f"{label}_member_sha256_invalid:{artifact_id}")
        if type(member.get("size_bytes")) is not int or member.get("size_bytes", 0) <= 0:
            errors.append(f"{label}_member_size_invalid:{artifact_id}")
        if not isinstance(member.get("artifact_type"), str) or not member.get("artifact_type"):
            errors.append(f"{label}_member_type_invalid:{artifact_id}")
        normalized_inventory[artifact_id] = member
    if candidate_status is FormalResultStatus.PASS and not normalized_inventory:
        errors.append(f"{label}_pass_member_inventory_empty")
    declared_inventory_sha = str(payload.get("member_inventory_sha256") or "")
    if (
        _SHA256.fullmatch(declared_inventory_sha) is None
        or declared_inventory_sha != scenario_member_inventory_sha256(inventory)
    ):
        errors.append(f"{label}_member_inventory_digest_mismatch")

    artifact_records = payload.get("artifact_records")
    if not isinstance(artifact_records, Mapping):
        errors.append(f"{label}_artifact_records_not_mapping")
        artifact_records = {}
    manifest_record = payload.get("manifest_record")
    if not isinstance(manifest_record, Mapping):
        errors.append(f"{label}_manifest_record_not_mapping")
        manifest_record = {}
    expected_manifest_id = f"native.manifest.{binding.scenario_id}"
    expected_inventory_ids = set(artifact_records) | ({expected_manifest_id} if manifest_record else set())
    if set(normalized_inventory) != expected_inventory_ids:
        errors.append(f"{label}_member_inventory_artifact_set_mismatch")
    records_to_check = dict(artifact_records)
    if manifest_record:
        records_to_check[expected_manifest_id] = manifest_record
    for artifact_id, record in records_to_check.items():
        member = normalized_inventory.get(str(artifact_id))
        if not isinstance(record, Mapping) or member is None:
            continue
        if set(record) != _ARTIFACT_RECORD_FIELDS:
            errors.append(f"{label}_record_shape_mismatch:{artifact_id}")
        if record.get("schema_version") != ARTIFACT_RECORD_SCHEMA_VERSION:
            errors.append(f"{label}_record_schema_mismatch:{artifact_id}")
        if record.get("artifact_id") != artifact_id:
            errors.append(f"{label}_record_id_mismatch:{artifact_id}")
        if record.get("scenario_id") != binding.scenario_id:
            errors.append(f"{label}_record_scenario_mismatch:{artifact_id}")
        if record.get("validated") is not True:
            errors.append(f"{label}_record_not_validated:{artifact_id}")
        if (
            record.get("sha256") != member.get("sha256")
            or record.get("size") != member.get("size_bytes")
            or record.get("type") != member.get("artifact_type")
        ):
            errors.append(f"{label}_record_inventory_mismatch:{artifact_id}")
        if record.get("type") not in {
            artifact_type.value
            for artifact_type in ArtifactType
            if artifact_type is not ArtifactType.AUTO
        }:
            errors.append(f"{label}_record_type_invalid:{artifact_id}")
        if not _canonical_path_text(record.get("path")):
            errors.append(f"{label}_record_path_not_canonical:{artifact_id}")
        if _receipt_utc(record.get("created_at"), f"{label}.{artifact_id}.created_at") is None:
            errors.append(f"{label}_record_created_at_invalid:{artifact_id}")
        required_true = (
            "mandatory",
            "scenario_link_valid",
            "within_artifact_root",
            "exists",
            "regular_file",
            "nonzero",
            "validation_snapshot_stable",
            "sha256_verified",
            "validated",
        )
        if any(record.get(field_name) is not True for field_name in required_true):
            errors.append(f"{label}_record_validation_flags_invalid:{artifact_id}")
        if (
            type(record.get("minimum_size_bytes")) is not int
            or record.get("minimum_size_bytes", 0) <= 0
            or not isinstance(record.get("expected_sha256"), str)
            or record.get("expected_sha256") not in {"", record.get("sha256")}
            or record.get("errors") != []
        ):
            errors.append(f"{label}_record_validation_metadata_invalid:{artifact_id}")
        format_validation = record.get("format_validation")
        secret_scan = record.get("secret_scan")
        if (
            not isinstance(format_validation, Mapping)
            or set(format_validation) != {"ok", "method", "details", "errors"}
            or format_validation.get("ok") is not True
            or not isinstance(format_validation.get("method"), str)
            or not format_validation.get("method")
            or not isinstance(format_validation.get("details"), Mapping)
            or format_validation.get("errors") != []
            or not isinstance(secret_scan, Mapping)
            or set(secret_scan) != {
                "schema_version",
                "performed",
                "coverage_complete",
                "ok",
                "scanned_bytes",
                "source_count",
                "pattern_count",
                "finding_count",
                "findings",
                "collector_errors",
            }
            or secret_scan.get("schema_version") != SECRET_SCAN_SCHEMA_VERSION
            or secret_scan.get("performed") is not True
            or secret_scan.get("coverage_complete") is not True
            or secret_scan.get("ok") is not True
            or type(secret_scan.get("scanned_bytes")) is not int
            or secret_scan.get("scanned_bytes", -1) < record.get("size", 0)
            or type(secret_scan.get("source_count")) is not int
            or secret_scan.get("source_count", 0) <= 0
            or type(secret_scan.get("pattern_count")) is not int
            or secret_scan.get("pattern_count", 0) <= 0
            or secret_scan.get("finding_count") != 0
            or secret_scan.get("findings") != []
            or secret_scan.get("collector_errors") != []
        ):
            errors.append(f"{label}_record_validation_evidence_invalid:{artifact_id}")
    if candidate_status is FormalResultStatus.PASS:
        if not manifest_record or manifest_record.get("validated") is not True:
            errors.append(f"{label}_pass_manifest_not_validated")
        if set(artifact_records) == set():
            errors.append(f"{label}_pass_artifact_records_empty")
    evidence_results = payload.get("evidence_adapter_results")
    if not isinstance(evidence_results, Mapping):
        errors.append(f"{label}_evidence_results_not_mapping")
        evidence_results = {}
    if set(evidence_results) != set(binding.evidence_adapter_ids):
        errors.append(f"{label}_evidence_result_set_mismatch")
    for evidence_id, adapter_id in binding.evidence_adapter_ids.items():
        result = evidence_results.get(evidence_id)
        if not isinstance(result, Mapping):
            continue
        observations = result.get("native_observation_ids")
        if (
            set(result) != _EVIDENCE_RESULT_FIELDS
            or
            result.get("evidence_id") != evidence_id
            or result.get("adapter_id") != adapter_id
            or type(result.get("validated")) is not bool
            or not isinstance(observations, list)
            or not all(isinstance(item, str) and item for item in observations)
        ):
            errors.append(f"{label}_evidence_result_invalid:{evidence_id}")
        if candidate_status is FormalResultStatus.PASS and (
            result.get("validated") is not True
            or not observations
            or result.get("failure_class") != ""
            or result.get("diagnostics") != []
        ):
            errors.append(f"{label}_pass_evidence_result_failed:{evidence_id}")
    validator_results = payload.get("validator_results")
    if not isinstance(validator_results, Mapping):
        errors.append(f"{label}_validator_results_not_mapping")
        validator_results = {}
    expected_validator_kinds = {
        **{item: ValidatorKind.TERMINAL.value for item in binding.terminal_validator_ids},
        **{item: ValidatorKind.CLEANUP.value for item in binding.cleanup_validator_ids},
        **{item: ValidatorKind.ARTIFACT.value for item in binding.artifact_validator_ids},
    }
    if set(validator_results) != set(expected_validator_kinds):
        errors.append(f"{label}_validator_result_set_mismatch")
    for validator_id, expected_kind in expected_validator_kinds.items():
        result = validator_results.get(validator_id)
        if not isinstance(result, Mapping):
            continue
        observations = result.get("native_observation_ids")
        if (
            set(result) != _VALIDATOR_RESULT_FIELDS
            or
            result.get("validator_id") != validator_id
            or result.get("kind") != expected_kind
            or type(result.get("passed")) is not bool
            or not isinstance(observations, list)
            or not all(isinstance(item, str) and item for item in observations)
        ):
            errors.append(f"{label}_validator_result_invalid:{validator_id}")
        if candidate_status is FormalResultStatus.PASS and (
            result.get("passed") is not True
            or not observations
            or result.get("failure_class") != ""
            or result.get("diagnostics") != []
        ):
            errors.append(f"{label}_pass_validator_result_failed:{validator_id}")
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, list) or not all(
        isinstance(item, str) and item for item in diagnostics
    ):
        errors.append(f"{label}_diagnostics_invalid")
    elif candidate_status is FormalResultStatus.PASS and diagnostics:
        errors.append(f"{label}_pass_has_diagnostics")
    return tuple(sorted(set(errors)))


def validate_scenario_runtime_receipt(
    receipt: object,
    binding: FormalScenarioBinding,
) -> RuntimeReceiptValidation:
    """Validate one future runner receipt; transport-only shortcuts always fail.

    The receipt is intentionally strict JSON.  Unknown fields are rejected so
    callers cannot smuggle ``ok``, ``rc=0``, HTTP 200/202, skips, or expected
    gaps into a formal PASS.
    """

    if not isinstance(receipt, Mapping) or not receipt:
        return RuntimeReceiptValidation(
            status=FormalResultStatus.FAIL_HARNESS,
            valid=False,
            contract_pass=False,
            errors=("runtime_receipt_empty_or_not_mapping",),
        )
    errors = _shortcut_errors(receipt)
    expected_fields = {
        "schema_version",
        "scenario_id",
        "runner_id",
        "status",
        "terminal_state",
        "authority",
        "evidence_receipts",
        "terminal_validator_results",
        "cleanup_validator_results",
        "artifact_validator_results",
        "artifact_ids",
        "artifact_bundle",
        "diagnostics",
    }
    missing = sorted(expected_fields - set(receipt))
    extra = sorted(set(receipt) - expected_fields)
    if missing:
        errors.append(f"runtime_receipt_fields_missing:{','.join(missing)}")
    if extra:
        errors.append(f"runtime_receipt_fields_extra:{','.join(extra)}")
    if receipt.get("schema_version") != RUNTIME_RECEIPT_SCHEMA_VERSION:
        errors.append("runtime_receipt_schema_mismatch")
    if receipt.get("scenario_id") != binding.scenario_id:
        errors.append("runtime_receipt_scenario_id_mismatch")
    if receipt.get("runner_id") != binding.runner_id:
        errors.append("runtime_receipt_runner_id_mismatch")
    try:
        declared_status = FormalResultStatus(receipt.get("status"))
    except (TypeError, ValueError):
        declared_status = FormalResultStatus.FAIL_HARNESS
        errors.append("runtime_receipt_status_invalid")
    terminal_state = receipt.get("terminal_state")
    if not isinstance(terminal_state, str) or not terminal_state.strip():
        errors.append("runtime_receipt_terminal_state_empty")
    errors.extend(scenario_authority_validation_errors(receipt.get("authority")))

    evidence_receipts = receipt.get("evidence_receipts")
    evidence_results: dict[str, bool] = {}
    if not isinstance(evidence_receipts, Mapping):
        errors.append("evidence_receipts_not_mapping")
    else:
        expected_evidence = set(binding.evidence_adapter_ids)
        actual_evidence = set(evidence_receipts)
        missing_evidence = sorted(expected_evidence - actual_evidence)
        extra_evidence = sorted(actual_evidence - expected_evidence)
        if missing_evidence:
            errors.append(f"evidence_receipts_missing:{','.join(missing_evidence)}")
        if extra_evidence:
            errors.append(f"evidence_receipts_extra:{','.join(extra_evidence)}")
        for evidence_id in sorted(expected_evidence & actual_evidence):
            evidence_receipt = evidence_receipts[evidence_id]
            if not isinstance(evidence_receipt, Mapping):
                errors.append(f"evidence_receipt_not_mapping:{evidence_id}")
                continue
            expected_evidence_fields = {
                "evidence_id",
                "adapter_id",
                "validated",
                "native_observation_ids",
            }
            if set(evidence_receipt) != expected_evidence_fields:
                errors.append(f"evidence_receipt_shape_mismatch:{evidence_id}")
                continue
            if evidence_receipt.get("evidence_id") != evidence_id:
                errors.append(f"evidence_receipt_id_mismatch:{evidence_id}")
            if evidence_receipt.get("adapter_id") != binding.evidence_adapter_ids[evidence_id]:
                errors.append(f"evidence_receipt_adapter_mismatch:{evidence_id}")
            if type(evidence_receipt.get("validated")) is not bool:
                errors.append(f"evidence_receipt_validated_not_boolean:{evidence_id}")
            else:
                evidence_results[evidence_id] = evidence_receipt["validated"]
            try:
                observations = _string_tuple(
                    evidence_receipt.get("native_observation_ids"),
                    f"native_observation_ids:{evidence_id}",
                    allow_empty=evidence_receipt.get("validated") is False,
                )
                if evidence_receipt.get("validated") is True and not observations:
                    errors.append(
                        f"evidence_receipt_observations_empty_or_invalid:{evidence_id}"
                    )
            except BindingValidationError:
                errors.append(f"evidence_receipt_observations_empty_or_invalid:{evidence_id}")

    validator_results: dict[str, bool] = {}
    for field_name, expected_ids in (
        ("terminal_validator_results", binding.terminal_validator_ids),
        ("cleanup_validator_results", binding.cleanup_validator_ids),
        ("artifact_validator_results", binding.artifact_validator_ids),
    ):
        field_errors, values = _exact_boolean_map(
            receipt.get(field_name), expected_ids, field_name
        )
        errors.extend(field_errors)
        validator_results.update(values)

    artifact_ids: tuple[str, ...] = ()
    try:
        artifact_ids = _string_tuple(
            receipt.get("artifact_ids"),
            "artifact_ids",
            allow_empty=declared_status is not FormalResultStatus.PASS,
        )
    except BindingValidationError:
        errors.append("runtime_receipt_artifact_ids_empty_or_invalid")
    bundle_reference_errors = _artifact_bundle_reference_errors(
        receipt.get("artifact_bundle"),
        scenario_id=binding.scenario_id,
        allow_empty_members=declared_status is not FormalResultStatus.PASS,
    )
    errors.extend(bundle_reference_errors)
    try:
        diagnostics = _string_tuple(
            receipt.get("diagnostics"), "diagnostics", allow_empty=True
        )
    except BindingValidationError:
        diagnostics = ()
        errors.append("runtime_receipt_diagnostics_invalid")

    if declared_status is FormalResultStatus.PASS:
        if terminal_state != "success":
            errors.append("runtime_receipt_terminal_state_not_success")
        if set(evidence_results) != set(binding.evidence_adapter_ids) or not all(
            evidence_results.values()
        ):
            errors.append("runtime_receipt_native_evidence_not_all_validated")
        expected_validator_ids = set(
            binding.terminal_validator_ids
            + binding.cleanup_validator_ids
            + binding.artifact_validator_ids
        )
        if set(validator_results) != expected_validator_ids or not all(
            validator_results.values()
        ):
            errors.append("runtime_receipt_validators_not_all_passed")
        if not artifact_ids:
            errors.append("runtime_receipt_artifacts_missing")
        elif artifact_ids != (f"native.artifact.bundle.{binding.scenario_id}",):
            errors.append("runtime_receipt_reviewed_artifact_bundle_missing")
        bundle_reference = receipt.get("artifact_bundle")
        if (
            not isinstance(bundle_reference, Mapping)
            or bundle_reference.get("artifact_id") not in artifact_ids
        ):
            errors.append("runtime_receipt_symbolic_bundle_without_concrete_reference")
        if diagnostics:
            errors.append("runtime_receipt_pass_has_diagnostics")
    elif not diagnostics:
        errors.append("runtime_receipt_nonpass_diagnostics_missing")

    if errors:
        return RuntimeReceiptValidation(
            status=FormalResultStatus.FAIL_HARNESS,
            valid=False,
            contract_pass=False,
            errors=tuple(sorted(set(errors))),
        )
    return RuntimeReceiptValidation(
        status=declared_status,
        valid=True,
        contract_pass=declared_status is FormalResultStatus.PASS,
        errors=(),
    )


_NATIVE_RUNNER_RESULT_FIELDS = frozenset({
    "schema_version",
    "scenario_id",
    "started_at",
    "finished_at",
    "elapsed_seconds",
    "terminal_state",
    "execution_succeeded",
    "ok",
    "steps",
    "artifacts",
    "formal_evidence_manifest",
})
_NATIVE_ARTIFACT_DECLARATION_FIELDS = frozenset({
    "artifact_id",
    "path",
    "artifact_type",
})
_NATIVE_MANIFEST_FIELDS = frozenset({
    "schema_version",
    "scenario_id",
    "artifact_ids",
    "evidence",
    "terminal",
    "cleanup",
})
_NATIVE_OBSERVATION_FIELDS = frozenset({
    "source_artifact_id",
    "json_pointer",
    "predicate",
    "expected",
})
_NATIVE_FORBIDDEN_SEMANTIC_KEYS = frozenset({
    "alias",
    "expected_gap",
    "fake",
    "fallback",
    "mock",
    "mocked",
    "runtime_receipt",
    "simulated",
    "skip",
    "skipped",
    "synthetic",
})
_NATIVE_FORBIDDEN_SEMANTIC_VALUES = frozenset({
    "alias",
    "expectedgap",
    "fake",
    "fallback",
    "mock",
    "mocked",
    "simulated",
    "skip",
    "skipped",
    "synthetic",
})
_WEAK_OBSERVATION_PATH_TOKENS = frozenset({
    "accepted",
    "http_status",
    "json_ok",
    "ok",
    "raw_json",
    "raw_ok",
    "rc",
    "return_code",
    "returncode",
    "status_code",
})
_NATIVE_OBSERVATION_PREDICATES = frozenset({
    "absent",
    "at_least",
    "contains",
    "equals",
    "greater_than_zero",
    "is_false",
    "is_true",
    "nonempty",
    "not_equals",
})
_MAX_NATIVE_ARTIFACTS = 256
_MAX_NATIVE_JSON_BYTES = 64 * 1024 * 1024
_MAX_NATIVE_MANIFEST_BYTES = 8 * 1024 * 1024


def _normalized_semantic_token(value: object) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _native_forbidden_semantic_errors(
    value: object,
    path: str = "native_manifest",
) -> list[str]:
    """Reject waiver/shortcut semantics before any assertion is evaluated."""

    errors: list[str] = []
    stack: list[tuple[object, str, int]] = [(value, path, 0)]
    visited = 0
    while stack:
        current, current_path, depth = stack.pop()
        visited += 1
        if visited > 200_000:
            errors.append("native_semantic_scan_node_limit_exceeded")
            break
        if depth > 128:
            errors.append(f"native_semantic_scan_depth_limit_exceeded:{current_path}")
            continue
        if isinstance(current, Mapping):
            for key, child in current.items():
                normalized_key = _normalized_semantic_token(key)
                if normalized_key in _NATIVE_FORBIDDEN_SEMANTIC_KEYS:
                    errors.append(f"native_shortcut_key_forbidden:{current_path}.{key}")
                stack.append((child, f"{current_path}.{key}", depth + 1))
        elif isinstance(current, (list, tuple)):
            for index, child in enumerate(current):
                stack.append((child, f"{current_path}[{index}]", depth + 1))
        elif isinstance(current, str):
            normalized = re.sub(r"[^a-z0-9]", "", current.lower())
            if normalized in _NATIVE_FORBIDDEN_SEMANTIC_VALUES:
                errors.append(f"native_shortcut_value_forbidden:{current_path}")
    return errors


def native_evidence_manifest_validation_errors(
    manifest: object,
    binding: FormalScenarioBinding,
    *,
    artifact_ids: Iterable[str],
) -> tuple[str, ...]:
    """Validate the complete in-memory native manifest without trusting a record."""

    label = "native_evidence_manifest"
    errors: list[str] = []
    if not isinstance(manifest, Mapping):
        return (f"{label}_not_mapping",)
    errors.extend(_native_forbidden_semantic_errors(manifest, label))
    if set(manifest) != _NATIVE_MANIFEST_FIELDS:
        errors.append(f"{label}_shape_mismatch")
    if manifest.get("schema_version") != NATIVE_EVIDENCE_MANIFEST_SCHEMA_VERSION:
        errors.append(f"{label}_schema_mismatch")
    if manifest.get("scenario_id") != binding.scenario_id:
        errors.append(f"{label}_scenario_mismatch")
    expected_artifact_ids = set(artifact_ids)
    declared_artifact_ids = manifest.get("artifact_ids")
    if (
        not isinstance(declared_artifact_ids, list)
        or not all(isinstance(item, str) for item in declared_artifact_ids)
        or len(declared_artifact_ids) != len(set(declared_artifact_ids))
        or set(declared_artifact_ids) != expected_artifact_ids
    ):
        errors.append(f"{label}_artifact_ids_mismatch")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, Mapping):
        errors.append(f"{label}_evidence_not_mapping")
    elif set(evidence) != set(binding.evidence_adapter_ids):
        errors.append(f"{label}_evidence_ids_mismatch")
    return tuple(sorted(set(errors)))


def _json_pointer_tokens(pointer: str) -> tuple[str, ...]:
    if not pointer.startswith("/"):
        raise BindingValidationError("native observation JSON pointer must start with '/'")
    tokens: list[str] = []
    for raw_token in pointer[1:].split("/"):
        if re.search(r"~(?:[^01]|$)", raw_token):
            raise BindingValidationError("native observation JSON pointer has invalid escape")
        tokens.append(raw_token.replace("~1", "/").replace("~0", "~"))
    if not tokens or any(not token for token in tokens):
        raise BindingValidationError("native observation JSON pointer contains an empty token")
    if any(
        _normalized_semantic_token(token) in _WEAK_OBSERVATION_PATH_TOKENS
        for token in tokens
    ):
        raise BindingValidationError(
            "native observation cannot use transport/process shortcut fields"
        )
    return tuple(tokens)


def _resolve_json_pointer(payload: object, pointer: str) -> tuple[bool, object]:
    try:
        tokens = _json_pointer_tokens(pointer)
    except BindingValidationError:
        raise
    current = payload
    for token in tokens:
        if isinstance(current, Mapping):
            if token not in current:
                return False, None
            current = current[token]
            continue
        if isinstance(current, list):
            if not token.isdigit():
                return False, None
            index = int(token)
            if index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


def _strict_scalar_equal(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    return actual == expected


def _evaluate_native_predicate(
    *,
    found: bool,
    actual: object,
    predicate: str,
    expected: object,
) -> tuple[bool, str]:
    if predicate == "absent":
        if expected is not None:
            return False, "absent_predicate_expected_must_be_null"
        return not found, "" if not found else "observed_value_was_present"
    if not found:
        return False, "json_pointer_not_found"
    if predicate == "equals":
        passed = _strict_scalar_equal(actual, expected)
    elif predicate == "not_equals":
        passed = not _strict_scalar_equal(actual, expected)
    elif predicate == "is_true":
        if expected is not None:
            return False, "is_true_predicate_expected_must_be_null"
        passed = actual is True
    elif predicate == "is_false":
        if expected is not None:
            return False, "is_false_predicate_expected_must_be_null"
        passed = actual is False
    elif predicate == "nonempty":
        if expected is not None:
            return False, "nonempty_predicate_expected_must_be_null"
        passed = actual not in (None, "", [], {})
    elif predicate == "greater_than_zero":
        if expected is not None:
            return False, "greater_than_zero_predicate_expected_must_be_null"
        passed = bool(
            not isinstance(actual, bool)
            and isinstance(actual, (int, float))
            and math.isfinite(float(actual))
            and float(actual) > 0.0
        )
    elif predicate == "at_least":
        if (
            isinstance(actual, bool)
            or isinstance(expected, bool)
            or not isinstance(actual, (int, float))
            or not isinstance(expected, (int, float))
            or not math.isfinite(float(actual))
            or not math.isfinite(float(expected))
        ):
            return False, "at_least_predicate_requires_finite_numbers"
        passed = float(actual) >= float(expected)
    elif predicate == "contains":
        try:
            passed = expected in actual  # type: ignore[operator]
        except (TypeError, ValueError):
            return False, "contains_predicate_requires_a_container"
    else:  # guarded by structural validation
        return False, "unsupported_native_predicate"
    return passed, "" if passed else "native_predicate_not_satisfied"


def _native_observation_result(
    observation: object,
    *,
    observation_scope: str,
    artifact_payloads: Mapping[str, object],
    artifact_sha256: Mapping[str, str],
) -> dict[str, Any]:
    if not isinstance(observation, Mapping):
        return {
            "structurally_valid": False,
            "passed": False,
            "observation_id": "",
            "diagnostics": [f"native_observation_not_mapping:{observation_scope}"],
        }
    errors: list[str] = []
    if set(observation) != _NATIVE_OBSERVATION_FIELDS:
        errors.append(f"native_observation_shape_mismatch:{observation_scope}")
    artifact_id = observation.get("source_artifact_id")
    pointer = observation.get("json_pointer")
    predicate = observation.get("predicate")
    expected = observation.get("expected")
    if not isinstance(artifact_id, str) or artifact_id not in artifact_payloads:
        errors.append(f"native_observation_source_unknown:{observation_scope}")
    if not isinstance(pointer, str):
        errors.append(f"native_observation_pointer_invalid:{observation_scope}")
    else:
        try:
            pointer_tokens = _json_pointer_tokens(pointer)
            if (
                not isinstance(expected, bool)
                and isinstance(expected, int)
                and expected in {200, 202}
                and any(
                    "status" in _normalized_semantic_token(token)
                    or "http" in _normalized_semantic_token(token)
                    for token in pointer_tokens
                )
            ):
                errors.append(
                    f"native_observation_http_acceptance_forbidden:{observation_scope}"
                )
        except BindingValidationError as exc:
            errors.append(f"native_observation_pointer_invalid:{observation_scope}:{exc}")
    if not isinstance(predicate, str) or predicate not in _NATIVE_OBSERVATION_PREDICATES:
        errors.append(f"native_observation_predicate_invalid:{observation_scope}")
    if errors:
        return {
            "structurally_valid": False,
            "passed": False,
            "observation_id": "",
            "diagnostics": errors,
        }
    assert isinstance(artifact_id, str)
    assert isinstance(pointer, str)
    assert isinstance(predicate, str)
    found, actual = _resolve_json_pointer(artifact_payloads[artifact_id], pointer)
    passed, diagnostic = _evaluate_native_predicate(
        found=found,
        actual=actual,
        predicate=predicate,
        expected=expected,
    )
    identity_material = json.dumps(
        {
            "scope": observation_scope,
            "artifact_id": artifact_id,
            "artifact_sha256": artifact_sha256[artifact_id],
            "json_pointer": pointer,
            "predicate": predicate,
            "expected": expected,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    observation_id = (
        f"native.observation.{hashlib.sha256(identity_material).hexdigest()}"
    )
    return {
        "structurally_valid": True,
        "passed": passed,
        "observation_id": observation_id,
        "diagnostics": [] if passed else [f"{diagnostic}:{observation_scope}"],
    }


def _evaluate_native_observations(
    observations: object,
    *,
    observation_scope: str,
    artifact_payloads: Mapping[str, object],
    artifact_sha256: Mapping[str, str],
) -> dict[str, Any]:
    if (
        isinstance(observations, (str, bytes))
        or not isinstance(observations, (list, tuple))
        or not observations
    ):
        return {
            "structurally_valid": False,
            "passed": False,
            "observation_ids": [],
            "diagnostics": [f"native_observations_empty_or_invalid:{observation_scope}"],
        }
    results = [
        _native_observation_result(
            observation,
            observation_scope=f"{observation_scope}[{index}]",
            artifact_payloads=artifact_payloads,
            artifact_sha256=artifact_sha256,
        )
        for index, observation in enumerate(observations)
    ]
    observation_ids = [
        str(result["observation_id"])
        for result in results
        if result.get("observation_id")
    ]
    return {
        "structurally_valid": all(
            result.get("structurally_valid") is True for result in results
        ),
        "passed": all(result.get("passed") is True for result in results),
        "observation_ids": observation_ids,
        "diagnostics": [
            str(item)
            for result in results
            for item in (result.get("diagnostics") or [])
        ],
    }


def strict_native_evidence_adapter(
    *,
    registration: NativeEvidenceAdapterRegistration,
    manifest: Mapping[str, Any],
    artifact_payloads: Mapping[str, object],
    artifact_sha256: Mapping[str, str],
) -> dict[str, Any]:
    """Re-evaluate one reviewed evidence item from immutable probe artifacts."""

    evidence = manifest.get("evidence")
    observations = evidence.get(registration.evidence_id) if isinstance(evidence, Mapping) else None
    evaluated = _evaluate_native_observations(
        observations,
        observation_scope=f"evidence.{registration.evidence_id}",
        artifact_payloads=artifact_payloads,
        artifact_sha256=artifact_sha256,
    )
    return {
        "evidence_id": registration.evidence_id,
        "adapter_id": registration.adapter_id,
        "validated": bool(
            evaluated["structurally_valid"] is True and evaluated["passed"] is True
        ),
        "native_observation_ids": list(evaluated["observation_ids"]),
        "failure_class": (
            ""
            if evaluated["structurally_valid"] is True and evaluated["passed"] is True
            else "FAIL_PRODUCT"
            if evaluated["structurally_valid"] is True
            else "FAIL_HARNESS"
        ),
        "diagnostics": list(evaluated["diagnostics"]),
    }


def _strict_native_state_validator(
    *,
    registration: ScenarioValidatorRegistration,
    manifest: Mapping[str, Any],
    manifest_field: str,
    expected_state: str,
    artifact_payloads: Mapping[str, object],
    artifact_sha256: Mapping[str, str],
) -> dict[str, Any]:
    block = manifest.get(manifest_field)
    diagnostics: list[str] = []
    structurally_valid = True
    observations: object = None
    state = None
    if not isinstance(block, Mapping) or set(block) != {"state", "observations"}:
        structurally_valid = False
        diagnostics.append(f"native_{manifest_field}_shape_mismatch")
    else:
        state = block.get("state")
        observations = block.get("observations")
        if state != expected_state:
            diagnostics.append(
                f"native_{manifest_field}_state_mismatch:{state!r}:{expected_state!r}"
            )
    evaluated = _evaluate_native_observations(
        observations,
        observation_scope=manifest_field,
        artifact_payloads=artifact_payloads,
        artifact_sha256=artifact_sha256,
    )
    structurally_valid = structurally_valid and evaluated["structurally_valid"] is True
    passed = bool(
        structurally_valid
        and state == expected_state
        and evaluated["passed"] is True
    )
    diagnostics.extend(evaluated["diagnostics"])
    return {
        "validator_id": registration.validator_id,
        "kind": registration.kind.value,
        "passed": passed,
        "native_observation_ids": list(evaluated["observation_ids"]),
        "failure_class": (
            "" if passed else "FAIL_PRODUCT" if structurally_valid else "FAIL_HARNESS"
        ),
        "diagnostics": diagnostics,
    }


def strict_native_terminal_validator(
    *,
    registration: ScenarioValidatorRegistration,
    manifest: Mapping[str, Any],
    artifact_payloads: Mapping[str, object],
    artifact_sha256: Mapping[str, str],
    **_unused: object,
) -> dict[str, Any]:
    return _strict_native_state_validator(
        registration=registration,
        manifest=manifest,
        manifest_field="terminal",
        expected_state="success",
        artifact_payloads=artifact_payloads,
        artifact_sha256=artifact_sha256,
    )


def strict_native_cleanup_validator(
    *,
    registration: ScenarioValidatorRegistration,
    manifest: Mapping[str, Any],
    artifact_payloads: Mapping[str, object],
    artifact_sha256: Mapping[str, str],
    **_unused: object,
) -> dict[str, Any]:
    return _strict_native_state_validator(
        registration=registration,
        manifest=manifest,
        manifest_field="cleanup",
        expected_state="clean",
        artifact_payloads=artifact_payloads,
        artifact_sha256=artifact_sha256,
    )


def strict_native_artifact_validator(
    *,
    registration: ScenarioValidatorRegistration,
    artifact_records: Mapping[str, Mapping[str, Any]],
    manifest_record: Mapping[str, Any],
    manifest: Mapping[str, Any],
    artifact_payloads: Mapping[str, object],
    artifact_sha256: Mapping[str, str],
    **_unused: object,
) -> dict[str, Any]:
    diagnostics = [
        f"native_artifact_validation_failed:{artifact_id}"
        for artifact_id, record in sorted(artifact_records.items())
        if record.get("validated") is not True
    ]
    if manifest_record.get("validated") is not True:
        diagnostics.append("native_evidence_manifest_artifact_validation_failed")
    summary_candidates = [
        (artifact_id, payload)
        for artifact_id, payload in artifact_payloads.items()
        if isinstance(payload, Mapping)
        and payload.get("schema_version") == NATIVE_EVIDENCE_SUMMARY_SCHEMA_VERSION
    ]
    if len(summary_candidates) != 1:
        diagnostics.append("native_evidence_summary_count_invalid")
    else:
        summary_id, summary = summary_candidates[0]
        expected_summary_fields = {
            "schema_version",
            "scenario_id",
            "source_artifact_sha256",
            "scenario_assertions",
            "terminal_assertions",
            "cleanup_assertions",
            "details",
        }
        if set(summary) != expected_summary_fields:
            diagnostics.append("native_evidence_summary_shape_mismatch")
        if summary.get("scenario_id") != registration.scenario_id:
            diagnostics.append("native_evidence_summary_scenario_mismatch")
        diagnostics.extend(
            _native_forbidden_semantic_errors(summary, "native_evidence_summary")
        )
        expected_source_hashes = {
            artifact_id: digest
            for artifact_id, digest in artifact_sha256.items()
            if artifact_id != summary_id
        }
        if summary.get("source_artifact_sha256") != dict(sorted(expected_source_hashes.items())):
            diagnostics.append("native_evidence_summary_source_hashes_mismatch")
        binding = FORMAL_SCENARIO_BINDINGS.get(registration.scenario_id)
        assertions = summary.get("scenario_assertions")
        if (
            binding is None
            or not isinstance(assertions, Mapping)
            or set(assertions) != set(binding.evidence_adapter_ids)
            or any(type(value) is not bool for value in assertions.values())
        ):
            diagnostics.append("native_evidence_summary_assertions_invalid")
        terminal_assertions = summary.get("terminal_assertions")
        cleanup_assertions = summary.get("cleanup_assertions")
        if (
            not isinstance(terminal_assertions, Mapping)
            or not terminal_assertions
            or any(type(value) is not bool for value in terminal_assertions.values())
        ):
            diagnostics.append("native_evidence_summary_terminal_assertions_invalid")
        if (
            not isinstance(cleanup_assertions, Mapping)
            or not cleanup_assertions
            or any(type(value) is not bool for value in cleanup_assertions.values())
        ):
            diagnostics.append("native_evidence_summary_cleanup_assertions_invalid")

        def pointer_token(value: str) -> str:
            return value.replace("~", "~0").replace("/", "~1")

        def canonical_observation(pointer: str) -> dict[str, object]:
            return {
                "source_artifact_id": summary_id,
                "json_pointer": pointer,
                "predicate": "is_true",
                "expected": None,
            }

        evidence_manifest = manifest.get("evidence")
        if binding is not None and isinstance(evidence_manifest, Mapping):
            for evidence_id in binding.evidence_adapter_ids:
                expected = [canonical_observation(
                    f"/scenario_assertions/{pointer_token(evidence_id)}"
                )]
                if evidence_manifest.get(evidence_id) != expected:
                    diagnostics.append(
                        f"native_evidence_manifest_noncanonical_evidence:{evidence_id}"
                    )
        else:
            diagnostics.append("native_evidence_manifest_evidence_unavailable")
        for manifest_field, summary_field, assertion_map in (
            ("terminal", "terminal_assertions", terminal_assertions),
            ("cleanup", "cleanup_assertions", cleanup_assertions),
        ):
            block = manifest.get(manifest_field)
            expected_observations = [
                canonical_observation(
                    f"/{summary_field}/{pointer_token(str(assertion_id))}"
                )
                for assertion_id in sorted(assertion_map)
            ] if isinstance(assertion_map, Mapping) else []
            if not isinstance(block, Mapping) or block.get("observations") != expected_observations:
                diagnostics.append(
                    f"native_evidence_manifest_noncanonical_{manifest_field}"
                )
    passed = not diagnostics and bool(artifact_records)
    return {
        "validator_id": registration.validator_id,
        "kind": registration.kind.value,
        "passed": passed,
        "native_observation_ids": [
            f"native.artifact.sha256.{record.get('sha256')}"
            for _artifact_id, record in sorted(artifact_records.items())
            if record.get("validated") is True and record.get("sha256")
        ],
        "failure_class": "" if passed else "FAIL_HARNESS",
        "diagnostics": diagnostics,
    }


def build_strict_native_adapter_registry(
    *,
    bindings: Mapping[str, FormalScenarioBinding] = FORMAL_SCENARIO_BINDINGS,
) -> Mapping[str, NativeEvidenceAdapterRegistration]:
    """Register the shared strict evaluator under every non-alias evidence ID."""

    implementation_ref = (
        f"{strict_native_evidence_adapter.__module__}:"
        f"{strict_native_evidence_adapter.__name__}"
    )
    registrations: dict[str, NativeEvidenceAdapterRegistration] = {}
    for scenario_id, binding in bindings.items():
        for evidence_id, adapter_id in binding.evidence_adapter_ids.items():
            registrations[adapter_id] = NativeEvidenceAdapterRegistration(
                adapter_id=adapter_id,
                scenario_id=scenario_id,
                evidence_id=evidence_id,
                implementation_ref=implementation_ref,
                handler=strict_native_evidence_adapter,
            )
    return MappingProxyType(registrations)


def build_strict_native_validator_registry(
    *,
    bindings: Mapping[str, FormalScenarioBinding] = FORMAL_SCENARIO_BINDINGS,
) -> Mapping[str, ScenarioValidatorRegistration]:
    """Register independent terminal, cleanup, and artifact validators."""

    handlers: Mapping[ValidatorKind, Callable[..., object]] = {
        ValidatorKind.TERMINAL: strict_native_terminal_validator,
        ValidatorKind.CLEANUP: strict_native_cleanup_validator,
        ValidatorKind.ARTIFACT: strict_native_artifact_validator,
    }
    registrations: dict[str, ScenarioValidatorRegistration] = {}
    for scenario_id, binding in bindings.items():
        for kind, validator_ids in (
            (ValidatorKind.TERMINAL, binding.terminal_validator_ids),
            (ValidatorKind.CLEANUP, binding.cleanup_validator_ids),
            (ValidatorKind.ARTIFACT, binding.artifact_validator_ids),
        ):
            handler = handlers[kind]
            implementation_ref = f"{handler.__module__}:{handler.__name__}"
            for validator_id in validator_ids:
                registrations[validator_id] = ScenarioValidatorRegistration(
                    validator_id=validator_id,
                    scenario_id=scenario_id,
                    kind=kind,
                    implementation_ref=implementation_ref,
                    handler=handler,
                )
    return MappingProxyType(registrations)


def strict_native_runtime_pipeline_verified(
    *,
    adapter_registry: Mapping[str, NativeEvidenceAdapterRegistration],
    validator_registry: Mapping[str, ScenarioValidatorRegistration],
    bindings: Mapping[str, FormalScenarioBinding] = FORMAL_SCENARIO_BINDINGS,
) -> bool:
    """Verify the runtime registries point at the reviewed strict handlers."""

    expected_adapters = _expected_adapter_bindings(bindings)
    expected_validators = _expected_validator_bindings(bindings)
    if set(adapter_registry) != set(expected_adapters):
        return False
    if set(validator_registry) != set(expected_validators):
        return False
    for adapter_id, (scenario_id, evidence_id) in expected_adapters.items():
        registration = adapter_registry.get(adapter_id)
        if not _registration_matches_adapter(
            registration,
            adapter_id=adapter_id,
            scenario_id=scenario_id,
            evidence_id=evidence_id,
        ):
            return False
        if registration.handler is not strict_native_evidence_adapter:
            return False
    handler_by_kind = {
        ValidatorKind.TERMINAL: strict_native_terminal_validator,
        ValidatorKind.CLEANUP: strict_native_cleanup_validator,
        ValidatorKind.ARTIFACT: strict_native_artifact_validator,
    }
    for validator_id, (scenario_id, kind) in expected_validators.items():
        registration = validator_registry.get(validator_id)
        if not _registration_matches_validator(
            registration,
            validator_id=validator_id,
            scenario_id=scenario_id,
            kind=kind,
        ):
            return False
        if registration.handler is not handler_by_kind[kind]:
            return False
    return True


def _path_component_symlink(path: Path, root: Path) -> bool:
    current = path
    while current != root:
        if current.is_symlink():
            return True
        if root not in current.parents:
            return True
        current = current.parent
    return root.is_symlink()


def _load_validated_native_artifacts(
    *,
    scenario_id: str,
    declarations: object,
    artifact_root: Path,
    known_secret_values: Mapping[str, str] | None,
) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[str, object],
    dict[str, str],
    list[str],
]:
    records: dict[str, Mapping[str, Any]] = {}
    payloads: dict[str, object] = {}
    digests: dict[str, str] = {}
    errors: list[str] = []
    if (
        isinstance(declarations, (str, bytes))
        or not isinstance(declarations, (list, tuple))
        or not declarations
    ):
        return records, payloads, digests, ["native_artifact_declarations_empty_or_invalid"]
    if len(declarations) > _MAX_NATIVE_ARTIFACTS:
        return records, payloads, digests, ["native_artifact_declaration_limit_exceeded"]
    seen_paths: set[Path] = set()
    for index, declaration in enumerate(declarations):
        scope = f"native_artifacts[{index}]"
        if not isinstance(declaration, Mapping):
            errors.append(f"native_artifact_declaration_not_mapping:{index}")
            continue
        if set(declaration) != _NATIVE_ARTIFACT_DECLARATION_FIELDS:
            errors.append(f"native_artifact_declaration_shape_mismatch:{index}")
            continue
        try:
            artifact_id = _identifier(declaration.get("artifact_id"), f"{scope}.artifact_id")
        except BindingValidationError:
            errors.append(f"native_artifact_id_invalid:{index}")
            continue
        if artifact_id in records:
            errors.append(f"native_artifact_id_duplicate:{artifact_id}")
            continue
        raw_path = declaration.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            errors.append(f"native_artifact_path_invalid:{artifact_id}")
            continue
        declared_path = Path(raw_path).expanduser()
        if not declared_path.is_absolute():
            errors.append(f"native_artifact_path_not_absolute:{artifact_id}")
            continue
        resolved = declared_path.resolve(strict=False)
        if resolved == artifact_root or artifact_root not in resolved.parents:
            errors.append(f"native_artifact_outside_root:{artifact_id}")
            continue
        if resolved in seen_paths:
            errors.append(f"native_artifact_path_duplicate:{artifact_id}")
            continue
        seen_paths.add(resolved)
        if _path_component_symlink(declared_path, artifact_root):
            errors.append(f"native_artifact_symlink_path_rejected:{artifact_id}")
            continue
        try:
            artifact_type = ArtifactType(str(declaration.get("artifact_type") or ""))
        except ValueError:
            errors.append(f"native_artifact_type_invalid:{artifact_id}")
            continue
        try:
            size = resolved.stat().st_size
        except OSError:
            size = 0
        is_json_artifact = bool(
            artifact_type is ArtifactType.JSON
            or (artifact_type is ArtifactType.AUTO and resolved.suffix.lower() == ".json")
        )
        if is_json_artifact and size > _MAX_NATIVE_JSON_BYTES:
            errors.append(f"native_json_artifact_too_large:{artifact_id}")
            continue
        spec = ArtifactSpec(
            artifact_id=artifact_id,
            scenario_id=scenario_id,
            path=declared_path,
            artifact_type=artifact_type,
        )
        record = validate_artifact(
            spec,
            known_scenario_ids={scenario_id},
            artifact_root=artifact_root,
            known_secret_values=known_secret_values,
        )
        records[artifact_id] = record
        if record.get("validated") is not True:
            errors.append(f"native_artifact_validation_failed:{artifact_id}")
            continue
        digest = str(record.get("sha256") or "")
        digests[artifact_id] = digest
        if record.get("type") == ArtifactType.JSON.value:
            try:
                first = json.loads(resolved.read_text(encoding="utf-8"))
                second = json.loads(resolved.read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(
                    f"native_json_artifact_reopen_failed:{artifact_id}:{exc.__class__.__name__}"
                )
                continue
            if first != second or not isinstance(first, (Mapping, list)):
                errors.append(f"native_json_artifact_unstable_or_scalar:{artifact_id}")
                continue
            payloads[artifact_id] = first
    return records, payloads, digests, errors


def _load_native_manifest(
    *,
    binding: FormalScenarioBinding,
    manifest_path_value: object,
    artifact_root: Path,
    artifact_ids: set[str],
    source_artifact_paths: set[Path],
    known_secret_values: Mapping[str, str] | None,
) -> tuple[dict[str, Any], Mapping[str, Any], list[str]]:
    errors: list[str] = []
    empty_record: dict[str, Any] = {}
    if not isinstance(manifest_path_value, str) or not manifest_path_value.strip():
        return {}, empty_record, ["native_evidence_manifest_path_missing"]
    declared_path = Path(manifest_path_value).expanduser()
    if not declared_path.is_absolute():
        return {}, empty_record, ["native_evidence_manifest_path_not_absolute"]
    resolved = declared_path.resolve(strict=False)
    if resolved == artifact_root or artifact_root not in resolved.parents:
        return {}, empty_record, ["native_evidence_manifest_outside_artifact_root"]
    if _path_component_symlink(declared_path, artifact_root):
        return {}, empty_record, ["native_evidence_manifest_symlink_path_rejected"]
    if resolved in source_artifact_paths:
        return {}, empty_record, ["native_evidence_manifest_cannot_be_source_artifact"]
    try:
        size = resolved.stat().st_size
    except OSError:
        size = 0
    if size > _MAX_NATIVE_MANIFEST_BYTES:
        return {}, empty_record, ["native_evidence_manifest_too_large"]
    manifest_artifact_id = f"native.manifest.{binding.scenario_id}"
    record = validate_artifact(
        ArtifactSpec(
            artifact_id=manifest_artifact_id,
            scenario_id=binding.scenario_id,
            path=declared_path,
            artifact_type=ArtifactType.JSON,
        ),
        known_scenario_ids={binding.scenario_id},
        artifact_root=artifact_root,
        known_secret_values=known_secret_values,
    )
    if record.get("validated") is not True:
        errors.append("native_evidence_manifest_artifact_validation_failed")
        return {}, record, errors
    try:
        first = json.loads(resolved.read_text(encoding="utf-8"))
        second = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"native_evidence_manifest_reopen_failed:{exc.__class__.__name__}")
        return {}, record, errors
    if first != second or not isinstance(first, Mapping):
        errors.append("native_evidence_manifest_unstable_or_not_mapping")
        return {}, record, errors
    manifest = dict(first)
    errors.extend(_native_forbidden_semantic_errors(manifest))
    if set(manifest) != _NATIVE_MANIFEST_FIELDS:
        errors.append("native_evidence_manifest_shape_mismatch")
    if manifest.get("schema_version") != NATIVE_EVIDENCE_MANIFEST_SCHEMA_VERSION:
        errors.append("native_evidence_manifest_schema_mismatch")
    if manifest.get("scenario_id") != binding.scenario_id:
        errors.append("native_evidence_manifest_scenario_mismatch")
    manifest_artifact_ids = manifest.get("artifact_ids")
    manifest_ids_valid = bool(
        not isinstance(manifest_artifact_ids, (str, bytes))
        and isinstance(manifest_artifact_ids, (list, tuple))
        and all(isinstance(item, str) for item in manifest_artifact_ids)
    )
    if manifest_ids_valid:
        manifest_id_list = list(manifest_artifact_ids)
        manifest_ids_valid = bool(
            len(manifest_id_list) == len(set(manifest_id_list))
            and set(manifest_id_list) == artifact_ids
        )
    if not manifest_ids_valid:
        errors.append("native_evidence_manifest_artifact_ids_mismatch")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, Mapping):
        errors.append("native_evidence_manifest_evidence_not_mapping")
    elif set(evidence) != set(binding.evidence_adapter_ids):
        errors.append("native_evidence_manifest_evidence_ids_mismatch")
    return manifest, record, errors


def _native_runner_result_errors(
    raw_result: object,
    binding: FormalScenarioBinding,
) -> list[str]:
    if not isinstance(raw_result, Mapping):
        return ["native_runner_result_not_mapping"]
    errors: list[str] = _native_forbidden_semantic_errors(
        raw_result, "native_runner_result"
    )
    if set(raw_result) != _NATIVE_RUNNER_RESULT_FIELDS:
        errors.append("native_runner_result_shape_mismatch")
    if raw_result.get("schema_version") != NATIVE_RUNNER_RESULT_SCHEMA_VERSION:
        errors.append("native_runner_result_schema_mismatch")
    if raw_result.get("scenario_id") != binding.scenario_id:
        errors.append("native_runner_result_scenario_mismatch")
    for field_name in ("started_at", "finished_at"):
        if not isinstance(raw_result.get(field_name), str) or not raw_result.get(field_name):
            errors.append(f"native_runner_result_{field_name}_invalid")
    elapsed = raw_result.get("elapsed_seconds")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(float(elapsed))
        or float(elapsed) < 0.0
    ):
        errors.append("native_runner_result_elapsed_invalid")
    if type(raw_result.get("execution_succeeded")) is not bool:
        errors.append("native_runner_result_execution_succeeded_not_boolean")
    if type(raw_result.get("ok")) is not bool:
        errors.append("native_runner_result_legacy_ok_not_boolean")
    elif raw_result.get("ok") is not raw_result.get("execution_succeeded"):
        errors.append("native_runner_result_process_signals_inconsistent")
    if raw_result.get("terminal_state") not in {"success", "failed", "interrupted"}:
        errors.append("native_runner_result_terminal_state_invalid")
    steps = raw_result.get("steps")
    if isinstance(steps, (str, bytes)) or not isinstance(steps, (list, tuple)) or not steps:
        errors.append("native_runner_steps_empty_or_invalid")
    else:
        step_ids: list[str] = []
        for index, step in enumerate(steps):
            if not isinstance(step, Mapping):
                errors.append(f"native_runner_step_not_mapping:{index}")
                continue
            step_id = step.get("step_id")
            if not isinstance(step_id, str) or not step_id:
                errors.append(f"native_runner_step_id_invalid:{index}")
            else:
                step_ids.append(step_id)
            if step.get("timed_out") is not False:
                errors.append(f"native_runner_step_timed_out_or_unknown:{index}")
            returncode = step.get("returncode")
            if isinstance(returncode, bool) or not isinstance(returncode, int):
                errors.append(f"native_runner_step_returncode_invalid:{index}")
            elif returncode != 0:
                errors.append(f"native_runner_step_process_failed:{index}")
            if step.get("evidence_errors") not in ([], ()):
                errors.append(f"native_runner_step_evidence_errors:{index}")
            if step.get("secret_leak_labels") not in ([], ()):
                errors.append(f"native_runner_step_secret_leak:{index}")
        if len(step_ids) != len(set(step_ids)):
            errors.append("native_runner_step_ids_duplicate")
    return errors


def _failed_adapter_result(
    registration: NativeEvidenceAdapterRegistration,
    diagnostics: Iterable[str],
) -> dict[str, Any]:
    return {
        "evidence_id": registration.evidence_id,
        "adapter_id": registration.adapter_id,
        "validated": False,
        "native_observation_ids": [],
        "failure_class": "FAIL_HARNESS",
        "diagnostics": list(diagnostics),
    }


def _failed_validator_result(
    registration: ScenarioValidatorRegistration,
    diagnostics: Iterable[str],
) -> dict[str, Any]:
    return {
        "validator_id": registration.validator_id,
        "kind": registration.kind.value,
        "passed": False,
        "native_observation_ids": [],
        "failure_class": "FAIL_HARNESS",
        "diagnostics": list(diagnostics),
    }


def _write_native_artifact_archive(
    *,
    artifact_root: Path,
    scenario_id: str,
    artifact_records: Mapping[str, Mapping[str, Any]],
    manifest_record: Mapping[str, Any],
) -> tuple[Path | None, str, int, list[dict[str, Any]], list[str]]:
    """Seal every validated native member into one immutable tar authority."""

    errors: list[str] = []
    inventory: list[dict[str, Any]] = []
    try:
        root = artifact_root.expanduser().resolve(strict=True)
        bundle_dir = root / "native_scenario_bundles"
        bundle_dir.mkdir(mode=0o700, exist_ok=True)
        if bundle_dir.is_symlink() or not bundle_dir.is_dir():
            return None, "", 0, [], ["native_artifact_archive_directory_invalid"]
        os.chmod(bundle_dir, 0o700)
        destination = bundle_dir / f"{scenario_id}.artifacts.tar"
        temporary = bundle_dir / (
            f".{scenario_id}.{os.getpid()}.{os.urandom(8).hex()}.archive.tmp"
        )
        sources: list[tuple[str, Mapping[str, Any]]] = [
            (str(key), value)
            for key, value in sorted(artifact_records.items())
            if isinstance(value, Mapping) and value.get("validated") is True
        ]
        if isinstance(manifest_record, Mapping) and manifest_record.get("validated") is True:
            sources.append((f"native.manifest.{scenario_id}", manifest_record))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                descriptor = -1
                with tarfile.open(
                    fileobj=output,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    for index, (artifact_id, record) in enumerate(sources):
                        source_text = str(record.get("path") or "")
                        source = Path(source_text)
                        if (
                            not _canonical_path_text(source_text)
                            or source.is_symlink()
                            or not source.is_file()
                        ):
                            errors.append(
                                f"native_artifact_archive_source_invalid:{artifact_id}"
                            )
                            continue
                        before = source.stat()
                        suffix = source.suffix
                        if not re.fullmatch(r"\.[A-Za-z0-9_.-]{1,32}", suffix or ""):
                            suffix = ".bin"
                        member_path = f"members/{index:04d}-{artifact_id}{suffix}"
                        info = tarfile.TarInfo(member_path)
                        info.size = int(before.st_size)
                        info.mode = 0o400
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = 0
                        with source.open("rb") as source_handle:
                            archive.addfile(info, source_handle)
                        after = source.stat()
                        actual_sha = hashlib.sha256()
                        with source.open("rb") as verify_handle:
                            for block in iter(lambda: verify_handle.read(1024 * 1024), b""):
                                actual_sha.update(block)
                        if (
                            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                            or record.get("size") != before.st_size
                            or record.get("sha256") != actual_sha.hexdigest()
                        ):
                            errors.append(
                                f"native_artifact_archive_source_changed:{artifact_id}"
                            )
                        inventory.append({
                            "artifact_id": artifact_id,
                            "member_path": member_path,
                            "sha256": str(record.get("sha256") or ""),
                            "size_bytes": int(record.get("size") or 0),
                            "artifact_type": str(record.get("type") or ""),
                        })
                output.flush()
                os.fsync(output.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            os.link(temporary, destination, follow_symlinks=False)
        finally:
            temporary.unlink(missing_ok=True)
        directory_fd = os.open(bundle_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        before_archive = destination.stat()
        reopened: dict[str, tuple[str, int]] = {}
        with tarfile.open(destination, mode="r:") as archive:
            for member in archive:
                if not member.isfile() or member.name in reopened:
                    errors.append("native_artifact_archive_member_type_or_duplicate_invalid")
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    errors.append("native_artifact_archive_member_unreadable")
                    continue
                digest = hashlib.sha256()
                size = 0
                for block in iter(lambda: extracted.read(1024 * 1024), b""):
                    size += len(block)
                    digest.update(block)
                reopened[member.name] = (digest.hexdigest(), size)
        expected_reopened = {
            str(item["member_path"]): (str(item["sha256"]), int(item["size_bytes"]))
            for item in inventory
        }
        if reopened != expected_reopened:
            errors.append("native_artifact_archive_reopen_inventory_mismatch")
        after_archive = destination.stat()
        if (
            before_archive.st_dev,
            before_archive.st_ino,
            before_archive.st_size,
            before_archive.st_mtime_ns,
        ) != (
            after_archive.st_dev,
            after_archive.st_ino,
            after_archive.st_size,
            after_archive.st_mtime_ns,
        ):
            errors.append("native_artifact_archive_changed_during_reopen")
        archive_digest = hashlib.sha256()
        with destination.open("rb") as archive_handle:
            for block in iter(lambda: archive_handle.read(1024 * 1024), b""):
                archive_digest.update(block)
        if not stat.S_ISREG(after_archive.st_mode) or after_archive.st_mode & 0o077:
            errors.append("native_artifact_archive_permissions_invalid")
        return (
            destination,
            archive_digest.hexdigest(),
            int(after_archive.st_size),
            inventory,
            errors,
        )
    except FileExistsError:
        return None, "", 0, [], ["native_artifact_archive_already_exists"]
    except Exception as exc:
        return None, "", 0, [], [
            f"native_artifact_archive_write_failed:{exc.__class__.__name__}"
        ]


def _write_native_artifact_bundle(
    *,
    artifact_root: Path,
    scenario_id: str,
    payload: Mapping[str, Any],
) -> tuple[Path | None, str, list[str]]:
    errors: list[str] = []
    try:
        declared_root = artifact_root.expanduser()
        declared_root.mkdir(parents=True, exist_ok=True)
        if declared_root.is_symlink():
            return None, "", ["native_artifact_root_symlink_rejected"]
        root = declared_root.resolve(strict=True)
        if not root.is_dir():
            return None, "", ["native_artifact_root_invalid"]
        bundle_dir = root / "native_scenario_bundles"
        bundle_dir.mkdir(mode=0o700, exist_ok=True)
        if bundle_dir.is_symlink() or not bundle_dir.is_dir():
            return None, "", ["native_artifact_bundle_directory_invalid"]
        os.chmod(bundle_dir, 0o700)
        destination = bundle_dir / f"{scenario_id}.json"
        encoded = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        temporary = bundle_dir / (
            f".{scenario_id}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, destination, follow_symlinks=False)
        finally:
            temporary.unlink(missing_ok=True)
        directory_fd = os.open(bundle_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        before = destination.stat()
        reopened = json.loads(destination.read_text(encoding="utf-8"))
        after = destination.stat()
        if reopened != payload:
            errors.append("native_artifact_bundle_reopen_mismatch")
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            errors.append("native_artifact_bundle_changed_during_reopen")
        if not stat.S_ISREG(after.st_mode) or after.st_mode & 0o077:
            errors.append("native_artifact_bundle_permissions_invalid")
        return destination, digest, errors
    except FileExistsError:
        return None, "", ["native_artifact_bundle_already_exists"]
    except Exception as exc:
        return None, "", [
            f"native_artifact_bundle_write_failed:{exc.__class__.__name__}"
        ]


def execute_registered_native_scenario(
    *,
    binding: FormalScenarioBinding,
    runner_registry: Mapping[str, ScenarioRunnerRegistration],
    adapter_registry: Mapping[str, NativeEvidenceAdapterRegistration],
    validator_registry: Mapping[str, ScenarioValidatorRegistration],
    artifact_root: Path,
    known_secret_values: Mapping[str, str] | None = None,
    authority: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Invoke a native runner and derive—never accept—a formal receipt.

    The caller supplies immutable campaign/source identity only; this function
    owns the wall and monotonic execution boundaries.  The runner may declare
    probe artifacts and a pointer manifest, but cannot return a formal receipt.
    Artifacts are independently reopened and every reviewed observation and
    terminal/cleanup/artifact validator is rerun before the receipt is built.
    """

    root = Path(artifact_root).expanduser().resolve(strict=False)
    runner = runner_registry.get(binding.runner_id)
    structural_errors: list[str] = []
    product_errors: list[str] = []
    authority_identity = dict(authority) if isinstance(authority, Mapping) else {}
    authority_errors = scenario_authority_identity_validation_errors(
        authority_identity,
        label="native_pipeline_authority_identity",
    )
    structural_errors.extend(authority_errors)
    authority_payload: dict[str, Any] = {}
    raw_result: object = None
    runner_invoked = False
    if not strict_native_runtime_pipeline_verified(
        adapter_registry=adapter_registry,
        validator_registry=validator_registry,
    ):
        structural_errors.append("native_runtime_registry_not_strict_verified")
    if not _registration_matches_runner(
        runner,
        runner_id=binding.runner_id,
        scenario_id=binding.scenario_id,
    ):
        structural_errors.append("native_runner_registration_missing_or_mismatched")
    elif not structural_errors:
        assert isinstance(runner, ScenarioRunnerRegistration)
        execution_started_at = datetime.now(timezone.utc)
        execution_started_monotonic_ns = time.monotonic_ns()
        try:
            runner_invoked = True
            raw_result = runner.handler()
        except Exception as exc:
            structural_errors.append(
                f"native_runner_exception:{exc.__class__.__name__}"
            )
        execution_finished_monotonic_ns = time.monotonic_ns()
        execution_finished_at = datetime.now(timezone.utc)
        if execution_finished_monotonic_ns <= execution_started_monotonic_ns:
            execution_finished_monotonic_ns = execution_started_monotonic_ns + 1
        if execution_finished_at <= execution_started_at:
            execution_finished_at = execution_started_at + timedelta(microseconds=1)
        authority_payload = {
            **authority_identity,
            "started_at": _precise_scenario_utc(execution_started_at),
            "finished_at": _precise_scenario_utc(execution_finished_at),
            "started_monotonic_ns": execution_started_monotonic_ns,
            "finished_monotonic_ns": execution_finished_monotonic_ns,
        }
    structural_errors.extend(_native_runner_result_errors(raw_result, binding))
    artifact_records: dict[str, Mapping[str, Any]] = {}
    artifact_payloads: dict[str, object] = {}
    artifact_sha256: dict[str, str] = {}
    manifest: dict[str, Any] = {}
    manifest_record: Mapping[str, Any] = {}
    if isinstance(raw_result, Mapping):
        runner_started = _receipt_utc(
            raw_result.get("started_at"),
            "native_runner_result.started_at",
        )
        runner_finished = _receipt_utc(
            raw_result.get("finished_at"),
            "native_runner_result.finished_at",
        )
        authority_started = _receipt_utc(
            authority_payload.get("started_at"),
            "native_pipeline_authority.started_at",
        )
        authority_finished = _receipt_utc(
            authority_payload.get("finished_at"),
            "native_pipeline_authority.finished_at",
        )
        boundary_tolerance = timedelta(seconds=2)
        if (
            runner_started is None
            or runner_finished is None
            or authority_started is None
            or authority_finished is None
            or runner_finished < runner_started
            or runner_started < authority_started - boundary_tolerance
            or runner_finished > authority_finished + boundary_tolerance
        ):
            structural_errors.append("native_runner_authority_wall_interval_mismatch")
        records, payloads, digests, artifact_errors = _load_validated_native_artifacts(
            scenario_id=binding.scenario_id,
            declarations=raw_result.get("artifacts"),
            artifact_root=root,
            known_secret_values=known_secret_values,
        )
        artifact_records = records
        artifact_payloads = payloads
        artifact_sha256 = digests
        structural_errors.extend(artifact_errors)
        manifest, manifest_record, manifest_errors = _load_native_manifest(
            binding=binding,
            manifest_path_value=raw_result.get("formal_evidence_manifest"),
            artifact_root=root,
            artifact_ids=set(artifact_records),
            source_artifact_paths={
                Path(str(record.get("path") or "")).resolve(strict=False)
                for record in artifact_records.values()
                if record.get("path")
            },
            known_secret_values=known_secret_values,
        )
        structural_errors.extend(manifest_errors)
        if raw_result.get("execution_succeeded") is not True:
            product_errors.append("native_runner_execution_not_successful")
        if raw_result.get("terminal_state") != "success":
            product_errors.append("native_runner_terminal_state_not_success")

    common_failure = tuple(sorted(set(structural_errors))) or (
        "native_manifest_unavailable",
    )
    evidence_results: dict[str, dict[str, Any]] = {}
    for evidence_id, adapter_id in binding.evidence_adapter_ids.items():
        registration = adapter_registry.get(adapter_id)
        if not _registration_matches_adapter(
            registration,
            adapter_id=adapter_id,
            scenario_id=binding.scenario_id,
            evidence_id=evidence_id,
        ):
            structural_errors.append(f"native_adapter_registration_missing:{adapter_id}")
            continue
        assert isinstance(registration, NativeEvidenceAdapterRegistration)
        if structural_errors or not manifest:
            result = _failed_adapter_result(registration, common_failure)
        else:
            try:
                result = registration.handler(
                    registration=registration,
                    manifest=manifest,
                    artifact_payloads=artifact_payloads,
                    artifact_sha256=artifact_sha256,
                )
            except Exception as exc:
                result = _failed_adapter_result(
                    registration,
                    (f"native_adapter_exception:{exc.__class__.__name__}",),
                )
        if not isinstance(result, Mapping):
            result = _failed_adapter_result(registration, ("native_adapter_result_invalid",))
        normalized = dict(result)
        if normalized.get("evidence_id") != evidence_id or normalized.get("adapter_id") != adapter_id:
            normalized = _failed_adapter_result(
                registration, ("native_adapter_result_identity_mismatch",)
            )
        evidence_results[evidence_id] = normalized

    validator_results: dict[str, dict[str, Any]] = {}
    expected_validator_bindings = (
        (ValidatorKind.TERMINAL, binding.terminal_validator_ids),
        (ValidatorKind.CLEANUP, binding.cleanup_validator_ids),
        (ValidatorKind.ARTIFACT, binding.artifact_validator_ids),
    )
    for kind, validator_ids in expected_validator_bindings:
        for validator_id in validator_ids:
            registration = validator_registry.get(validator_id)
            if not _registration_matches_validator(
                registration,
                validator_id=validator_id,
                scenario_id=binding.scenario_id,
                kind=kind,
            ):
                structural_errors.append(
                    f"native_validator_registration_missing:{validator_id}"
                )
                continue
            assert isinstance(registration, ScenarioValidatorRegistration)
            if structural_errors or not manifest:
                result = _failed_validator_result(registration, common_failure)
            else:
                try:
                    result = registration.handler(
                        registration=registration,
                        manifest=manifest,
                        artifact_payloads=artifact_payloads,
                        artifact_sha256=artifact_sha256,
                        artifact_records=artifact_records,
                        manifest_record=manifest_record,
                    )
                except Exception as exc:
                    result = _failed_validator_result(
                        registration,
                        (f"native_validator_exception:{exc.__class__.__name__}",),
                    )
            if not isinstance(result, Mapping):
                result = _failed_validator_result(
                    registration, ("native_validator_result_invalid",)
                )
            normalized = dict(result)
            if (
                normalized.get("validator_id") != validator_id
                or normalized.get("kind") != kind.value
            ):
                normalized = _failed_validator_result(
                    registration, ("native_validator_result_identity_mismatch",)
                )
            validator_results[validator_id] = normalized

    for result in evidence_results.values():
        if result.get("validated") is not True:
            target = structural_errors if result.get("failure_class") == "FAIL_HARNESS" else product_errors
            target.extend(str(item) for item in (result.get("diagnostics") or []))
    for result in validator_results.values():
        if result.get("passed") is not True:
            target = structural_errors if result.get("failure_class") == "FAIL_HARNESS" else product_errors
            target.extend(str(item) for item in (result.get("diagnostics") or []))

    missing_evidence_results = sorted(set(binding.evidence_adapter_ids) - set(evidence_results))
    expected_validator_ids = set(
        binding.terminal_validator_ids
        + binding.cleanup_validator_ids
        + binding.artifact_validator_ids
    )
    missing_validator_results = sorted(expected_validator_ids - set(validator_results))
    if missing_evidence_results:
        structural_errors.append(
            f"native_evidence_results_missing:{','.join(missing_evidence_results)}"
        )
    if missing_validator_results:
        structural_errors.append(
            f"native_validator_results_missing:{','.join(missing_validator_results)}"
        )

    (
        archive_path,
        archive_sha256,
        archive_size,
        member_inventory,
        archive_errors,
    ) = _write_native_artifact_archive(
        artifact_root=root,
        scenario_id=binding.scenario_id,
        artifact_records=artifact_records,
        manifest_record=manifest_record,
    )
    structural_errors.extend(archive_errors)
    initial_status = (
        FormalResultStatus.FAIL_HARNESS
        if structural_errors
        else FormalResultStatus.FAIL_PRODUCT
        if product_errors
        else FormalResultStatus.PASS
    )
    bundle_payload = {
        "schema_version": NATIVE_ARTIFACT_BUNDLE_SCHEMA_VERSION,
        "pipeline_schema_version": NATIVE_RUNTIME_PIPELINE_SCHEMA_VERSION,
        "scenario_id": binding.scenario_id,
        "runner_id": binding.runner_id,
        "candidate_status": initial_status.value,
        "authority": authority_payload,
        "artifact_records": {
            key: dict(value) for key, value in sorted(artifact_records.items())
        },
        "manifest_record": dict(manifest_record),
        "artifact_archive": {
            "artifact_id": f"native.artifact.archive.{binding.scenario_id}",
            "content_schema_version": NATIVE_ARTIFACT_ARCHIVE_SCHEMA_VERSION,
            "path": str(archive_path) if archive_path else "/invalid/native-artifact-archive",
            "sha256": archive_sha256,
            "size_bytes": archive_size,
            "media_type": "application/x-tar",
        },
        "member_inventory": member_inventory,
        "member_inventory_sha256": scenario_member_inventory_sha256(
            member_inventory
        ),
        "evidence_adapter_results": {
            key: value for key, value in sorted(evidence_results.items())
        },
        "validator_results": {
            key: value for key, value in sorted(validator_results.items())
        },
        "diagnostics": sorted(set(structural_errors + product_errors)),
    }
    bundle_schema_errors = native_artifact_bundle_validation_errors(
        bundle_payload,
        binding,
        expected_authority=authority_payload,
    )
    structural_errors.extend(bundle_schema_errors)
    if bundle_schema_errors:
        bundle_payload["candidate_status"] = FormalResultStatus.FAIL_HARNESS.value
        bundle_payload["diagnostics"] = sorted(
            set(structural_errors + product_errors)
        )
    bundle_path, bundle_sha256, bundle_errors = _write_native_artifact_bundle(
        artifact_root=root,
        scenario_id=binding.scenario_id,
        payload=bundle_payload,
    )
    structural_errors.extend(bundle_errors)
    status = (
        FormalResultStatus.FAIL_HARNESS
        if structural_errors
        else FormalResultStatus.FAIL_PRODUCT
        if product_errors
        else FormalResultStatus.PASS
    )
    diagnostics = sorted(set(structural_errors + product_errors))
    manifest_sha256 = str(manifest_record.get("sha256") or hashlib.sha256(b"").hexdigest())
    bundle_size = int(bundle_path.stat().st_size) if bundle_path is not None else 0
    receipt = {
        "schema_version": RUNTIME_RECEIPT_SCHEMA_VERSION,
        "scenario_id": binding.scenario_id,
        "runner_id": binding.runner_id,
        "status": status.value,
        "terminal_state": "success" if status is FormalResultStatus.PASS else "failed",
        "authority": authority_payload,
        "evidence_receipts": {
            evidence_id: {
                "evidence_id": evidence_id,
                "adapter_id": adapter_id,
                "validated": evidence_results.get(evidence_id, {}).get("validated") is True,
                "native_observation_ids": list(
                    evidence_results.get(evidence_id, {}).get("native_observation_ids") or []
                ),
            }
            for evidence_id, adapter_id in binding.evidence_adapter_ids.items()
        },
        "terminal_validator_results": {
            validator_id: validator_results.get(validator_id, {}).get("passed") is True
            for validator_id in binding.terminal_validator_ids
        },
        "cleanup_validator_results": {
            validator_id: validator_results.get(validator_id, {}).get("passed") is True
            for validator_id in binding.cleanup_validator_ids
        },
        "artifact_validator_results": {
            validator_id: validator_results.get(validator_id, {}).get("passed") is True
            for validator_id in binding.artifact_validator_ids
        },
        "artifact_ids": (
            [f"native.artifact.bundle.{binding.scenario_id}"] if bundle_path else []
        ),
        "artifact_bundle": {
            "artifact_id": f"native.artifact.bundle.{binding.scenario_id}",
            "content_schema_version": NATIVE_ARTIFACT_BUNDLE_SCHEMA_VERSION,
            "path": str(bundle_path) if bundle_path else "/invalid/native-artifact-bundle",
            "sha256": bundle_sha256,
            "size_bytes": bundle_size,
            "manifest_sha256": manifest_sha256,
            "member_inventory_sha256": scenario_member_inventory_sha256(
                member_inventory
            ),
            "member_count": len(member_inventory),
            "artifact_archive_id": f"native.artifact.archive.{binding.scenario_id}",
            "artifact_archive_sha256": archive_sha256,
            "artifact_archive_size_bytes": archive_size,
        },
        "diagnostics": diagnostics,
    }
    receipt_validation = validate_scenario_runtime_receipt(receipt, binding)
    if not receipt_validation.valid:
        status = FormalResultStatus.FAIL_HARNESS
        diagnostics = sorted(set(diagnostics + list(receipt_validation.errors)))
    return {
        "schema_version": NATIVE_RUNTIME_PIPELINE_SCHEMA_VERSION,
        "ok": bool(status is FormalResultStatus.PASS and receipt_validation.contract_pass),
        "classification": status.value,
        "scenario_id": binding.scenario_id,
        "runner_id": binding.runner_id,
        "runner_invoked": runner_invoked,
        "runtime_receipt": receipt,
        "runtime_receipt_validation": receipt_validation.to_dict(),
        "artifact_bundle_path": str(bundle_path) if bundle_path else "",
        "artifact_bundle_sha256": bundle_sha256,
        "artifact_bundle_size_bytes": bundle_size,
        "artifact_archive_path": str(archive_path) if archive_path else "",
        "artifact_archive_sha256": archive_sha256,
        "artifact_archive_size_bytes": archive_size,
        "diagnostics": diagnostics,
    }


__all__ = [
    "AUDITED_NATIVE_BINDING_BLOCKERS",
    "FORMAL_BINDING_GATE_SCHEMA_VERSION",
    "FORMAL_BINDING_SCHEMA_VERSION",
    "FORMAL_SCENARIO_BINDINGS",
    "NATIVE_ARTIFACT_ARCHIVE_SCHEMA_VERSION",
    "NATIVE_ARTIFACT_BUNDLE_SCHEMA_VERSION",
    "NATIVE_EVIDENCE_MANIFEST_SCHEMA_VERSION",
    "NATIVE_EVIDENCE_SUMMARY_SCHEMA_VERSION",
    "NATIVE_RUNNER_RESULT_SCHEMA_VERSION",
    "NATIVE_RUNTIME_PIPELINE_SCHEMA_VERSION",
    "RUNTIME_RECEIPT_SCHEMA_VERSION",
    "BindingValidationError",
    "FormalBindingGateResult",
    "FormalScenarioBinding",
    "NativeEvidenceAdapterRegistration",
    "RuntimeReceiptValidation",
    "ScenarioRunnerRegistration",
    "ScenarioValidatorRegistration",
    "ValidatorKind",
    "build_and_validate_formal_scenario_bindings",
    "build_formal_scenario_contracts",
    "build_strict_native_adapter_registry",
    "build_strict_native_validator_registry",
    "execute_registered_native_scenario",
    "native_artifact_bundle_validation_errors",
    "native_evidence_manifest_validation_errors",
    "scenario_registration_coverage",
    "scenario_authority_validation_errors",
    "scenario_authority_identity_validation_errors",
    "scenario_member_inventory_sha256",
    "strict_native_artifact_validator",
    "strict_native_cleanup_validator",
    "strict_native_evidence_adapter",
    "strict_native_runtime_pipeline_verified",
    "strict_native_terminal_validator",
    "validate_formal_scenario_bindings",
    "validate_scenario_runtime_receipt",
]
