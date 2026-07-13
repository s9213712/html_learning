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
from enum import Enum
import math
import re
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

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
RUNTIME_RECEIPT_SCHEMA_VERSION = "hackme.campaign.native-scenario-receipt/v1"
_FORMAL_DURATION_SECONDS = 86_400.0
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,159}$")
_IMPLEMENTATION_REF = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$"
)


# These blockers are an audit of the pre-existing operational campaign
# methods, not waivers.  Keeping them beside the reviewed binding manifest
# makes the gate explain *why* a callable has not been registered instead of
# reducing the failure to a very long list of opaque IDs.  Every reviewed
# scenario stays mandatory and every tuple must become empty before a formal
# binding may be considered complete.
AUDITED_NATIVE_BINDING_BLOCKERS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "media_long_hls_share": (
        "primary_planned_restart_not_executed_by_legacy_runner",
        "post_restart_hls_share_continuity_not_observed",
        "independent_cleanup_and_artifact_validators_missing",
    ),
    "cloud_drive_share_stream": (
        "native_scenario_runner_missing",
        "cloud_stream_terminal_share_revoke_evidence_adapters_missing",
    ),
    "bt_download_stream_restart": (
        "native_scenario_runner_missing",
        "magnet_torrent_hash_pause_resume_restart_evidence_adapters_missing",
    ),
    "ai_agent_positive_operations": (
        "legacy_ai_probes_contain_expected_gap_semantics",
        "positive_write_operations_and_scheduled_restart_receipt_missing",
        "independent_cleanup_and_artifact_validators_missing",
    ),
    "comfyui_real_workflows": (
        "dedicated_real_backend_runner_missing",
        "official_custom_workflow_terminal_output_and_cleanup_evidence_missing",
    ),
    "trading_background_custom_workflow": (
        "legacy_trading_runner_has_rc_only_step_without_machine_artifact",
        "lending_bot_and_custom_workflow_terminal_side_effect_evidence_missing",
        "independent_cleanup_and_artifact_validators_missing",
    ),
    "pointschain_hft_invariants": (
        "branch_and_dispute_evidence_not_part_of_legacy_hft_runner",
        "scenario_account_and_state_cleanup_validator_missing",
        "independent_artifact_bundle_validator_missing",
    ),
    "wallet_incident_governance": (
        "native_reviewed_scenario_runner_missing",
        "wallet_freeze_risk_vote_compensation_recovery_receipt_not_unified",
    ),
    "backup_restore_restart": (
        "native_reviewed_scenario_runner_missing",
        "legacy_cli_backup_restore_steps_lack_machine_success_artifacts",
        "point_in_time_manifest_archive_readability_and_cleanup_evidence_missing",
    ),
    "server_emergency_incident": (
        "native_scenario_runner_missing",
        "legacy_ai_probe_rejects_incident_write_instead_of_enter_resolve_cycle",
    ),
    "media_proxy_cross_browser": (
        "formal_browser_invocation_does_not_require_all_browsers",
        "subtitle_switch_evidence_missing",
        "chat_fixture_cleanup_validator_missing",
        "independent_artifact_bundle_validator_missing",
    ),
    "community_governance_operations": (
        "native_reviewed_scenario_runner_missing",
        "legacy_governance_probe_contains_expected_gap_semantics",
        "proposal_vote_execute_and_fixture_cleanup_evidence_missing",
    ),
    "final_ui_mobile_prelaunch": (
        "legacy_deep_site_and_production_gate_steps_lack_machine_artifacts",
        "all_feature_navigation_and_44px_touch_assertion_receipts_missing",
        "independent_cleanup_and_artifact_bundle_validators_missing",
    ),
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
    elif not isinstance(value, bool) and isinstance(value, int) and value in {200, 202}:
        errors.append(f"http_acceptance_code_forbidden:{path}")
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
        "evidence_receipts",
        "terminal_validator_results",
        "cleanup_validator_results",
        "artifact_validator_results",
        "artifact_ids",
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
                _string_tuple(
                    evidence_receipt.get("native_observation_ids"),
                    f"native_observation_ids:{evidence_id}",
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
        artifact_ids = _string_tuple(receipt.get("artifact_ids"), "artifact_ids")
    except BindingValidationError:
        errors.append("runtime_receipt_artifact_ids_empty_or_invalid")
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
        elif f"native.artifact.bundle.{binding.scenario_id}" not in artifact_ids:
            errors.append("runtime_receipt_reviewed_artifact_bundle_missing")
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


__all__ = [
    "AUDITED_NATIVE_BINDING_BLOCKERS",
    "FORMAL_BINDING_GATE_SCHEMA_VERSION",
    "FORMAL_BINDING_SCHEMA_VERSION",
    "FORMAL_SCENARIO_BINDINGS",
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
    "scenario_registration_coverage",
    "validate_formal_scenario_bindings",
    "validate_scenario_runtime_receipt",
]
