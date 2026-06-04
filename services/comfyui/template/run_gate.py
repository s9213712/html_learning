"""§10 Run-time 5-gate enforcement + §10.3 implementation notes.

Spec reference: docs/comfyui/COMFYUI_TEMPLATE_IMPORTER_PLAN.md §10 / §10.3.

This module is a *pure* gate runner — it never queues to ComfyUI itself.
The route handler calls ``run_workflow_through_gates(...)`` and on success
receives the patched workflow ready to be passed to the existing
queue helper. On failure it gets a ``RunGateFailure`` carrying the failed
gate index, stage tag, message, and audit metadata so the route can emit
``COMFYUI_TEMPLATE_RUN_GATE_FAIL`` (per §10.3.1) and return a stage-tagged
error to the user.

§10.3 implementation notes covered here:
1. Each gate failure produces a structured failure record; the route is
   expected to audit each one before returning.
2. Media upload to ComfyUI input/<run_id>/ is delegated to the caller-
   supplied ``upload_callback`` — when Gate 5 raises after some images
   were uploaded, the caller is expected to call ``cleanup_callback`` to
   reap that run_id's subfolder (the caller knows the filesystem layout).
3. ``apply_user_inputs`` patches user_input scalars *only on inputs that
   are in the analysis's user_inputs list and not in PROTECTED_INPUTS*,
   guaranteeing no overwrite of the LoadImage/LoadImageMask/LoadVideo remap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from services.comfyui.template.analyzer import (
    FieldCategory,
    InputField,
    WorkflowAnalysis,
    analyze_workflow_json,
)
from services.comfyui.template.capability import (
    CapabilityCheck,
    check_workflow_capability,
    rewrite_workflow_model_inputs_to_local_options,
)
from services.comfyui.template.cleanup import register_run_dir
from services.comfyui.template import errors as template_errors
from services.comfyui.template.remap import (
    PROTECTED_MEDIA_INPUTS,
    UploadCallback,
    remap_load_image_to_cloud_file,
)
from services.comfyui.template.safety import (
    SafetyError,
    enforce_allowlist,
    rewrite_save_image_prefix,
)
from services.comfyui.workflow.compat import apply_workflow_compatibility_fixes
from services.comfyui.validation.rules import WorkflowValidationError
from services.comfyui.validation.sanitize import sanitize_workflow_json


# ----------------------------------------------------------------------------
# Result containers
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RunGateFailure(Exception):
    """Raised when any of the five gates rejects the run."""

    gate: int
    stage: str
    msg: str
    audit_detail: dict[str, Any] = field(default_factory=dict)
    http_status: int = 400

    def __str__(self) -> str:  # pragma: no cover - dataclass cosmetic
        return f"gate={self.gate} stage={self.stage} msg={self.msg}"


@dataclass
class RunGateResult:
    """Return type on the happy path."""

    workflow: dict[str, Any]
    analysis: WorkflowAnalysis
    capability: CapabilityCheck
    audit_metadata: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Internal validators
# ----------------------------------------------------------------------------


_EMBEDDING_TOKEN_RE = re.compile(r"<\s*embeddings\s*:\s*([^<>]+?)\s*>", re.IGNORECASE)

_TEMPLATE_LOCKED_MODEL_FIELDS = {
    ("VAELoader", "vae_name"),
    ("CLIPLoader", "clip_name"),
    ("DualCLIPLoader", "clip_name1"),
    ("DualCLIPLoader", "clip_name2"),
    ("TripleCLIPLoader", "clip_name1"),
    ("TripleCLIPLoader", "clip_name2"),
    ("TripleCLIPLoader", "clip_name3"),
    ("CLIPLoaderGGUF", "clip_name"),
    ("DualCLIPLoaderGGUF", "clip_name1"),
    ("DualCLIPLoaderGGUF", "clip_name2"),
    ("TripleCLIPLoaderGGUF", "clip_name1"),
    ("TripleCLIPLoaderGGUF", "clip_name2"),
    ("TripleCLIPLoaderGGUF", "clip_name3"),
    ("CLIPVisionLoader", "clip_name"),
    ("UNETLoader", "unet_name"),
    ("UnetLoaderGGUF", "unet_name"),
    ("UnetLoaderGGUFAdvanced", "unet_name"),
    ("LatentUpscaleModelLoader", "model_name"),
}


def _normalize_embedding_shortcuts(value: Any) -> Any:
    if not isinstance(value, str) or "<" not in value:
        return value
    return _EMBEDDING_TOKEN_RE.sub(
        lambda match: f"embedding:{match.group(1).strip()}",
        value,
    )


def _is_user_editable(field_obj: InputField) -> bool:
    """Mirror of services/comfyui/template/ui_schema.required_user_inputs filtering."""
    if (field_obj.class_type, field_obj.input_name) in {
        ("SaveImage", "filename_prefix"),
        ("SaveVideo", "filename_prefix"),
        ("SaveAudio", "filename_prefix"),
        ("SaveAudioMP3", "filename_prefix"),
    }:
        return False
    if (field_obj.class_type, field_obj.input_name) in PROTECTED_MEDIA_INPUTS:
        # protected media inputs go through remap, not user_inputs
        return False
    if (field_obj.class_type, field_obj.input_name) in _TEMPLATE_LOCKED_MODEL_FIELDS:
        return False
    if field_obj.category == FieldCategory.UNKNOWN:
        return False
    return True


def _required_input_keys(analysis: WorkflowAnalysis) -> set[tuple[str, str]]:
    return {
        (f.node_id, f.input_name)
        for f in analysis.user_inputs
        if _is_user_editable(f)
    }


def _check_user_input_constraints(
    analysis: WorkflowAnalysis,
    user_inputs: Mapping[str, Any],
) -> None:
    """Gate 4: required + numeric / text size constraints.

    user_inputs shape: ``{node_id: {input_name: value}}`` — same as
    ``apply_user_inputs`` consumes. Missing protected node assignments are
    NOT checked here (they live in image_field_assignments and are
    enforced inside Gate 5's remap).
    """
    required = _required_input_keys(analysis)
    seen: set[tuple[str, str]] = set()

    def _constraint_fail(detail: str, **audit_extra) -> RunGateFailure:
        stage, msg = template_errors.gate4_constraints_msg(detail)
        return RunGateFailure(
            gate=4, stage=stage, msg=msg,
            audit_detail=audit_extra,
        )

    for node_id, patch in (user_inputs or {}).items():
        if not isinstance(patch, Mapping):
            raise _constraint_fail(
                f"user_inputs[{node_id}] 必須是物件",
                node_id=str(node_id), type=type(patch).__name__,
            )
        for input_name, value in patch.items():
            seen.add((str(node_id), str(input_name)))
            field_obj = next(
                (
                    f
                    for f in analysis.user_inputs
                    if f.node_id == str(node_id) and f.input_name == str(input_name)
                ),
                None,
            )
            if field_obj is None:
                raise _constraint_fail(
                    f"user_inputs[{node_id}].{input_name} 不在可編輯欄位清單中",
                    node_id=str(node_id), input_name=str(input_name),
                )
            # Type / range checks. Spec says "numeric / enum / size".
            if field_obj.category == FieldCategory.NUMERIC:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise _constraint_fail(
                        f"user_inputs[{node_id}].{input_name} 必須是數值，"
                        f"目前型別為 {type(value).__name__}",
                        node_id=str(node_id), input_name=str(input_name),
                    )
            elif field_obj.category == FieldCategory.BOOLEAN:
                if not isinstance(value, bool):
                    raise _constraint_fail(
                        f"user_inputs[{node_id}].{input_name} 必須是布林值，"
                        f"目前型別為 {type(value).__name__}",
                        node_id=str(node_id), input_name=str(input_name),
                    )
            elif field_obj.category == FieldCategory.TEXT:
                if not isinstance(value, str):
                    raise _constraint_fail(
                        f"user_inputs[{node_id}].{input_name} 必須是字串，"
                        f"目前型別為 {type(value).__name__}",
                        node_id=str(node_id), input_name=str(input_name),
                    )
                if len(value) > 4000:
                    raise _constraint_fail(
                        f"user_inputs[{node_id}].{input_name} 長度超過 4000 字元",
                        node_id=str(node_id), input_name=str(input_name), len=len(value),
                    )
            elif field_obj.category == FieldCategory.SAMPLER:
                if not isinstance(value, str):
                    raise _constraint_fail(
                        f"user_inputs[{node_id}].{input_name} 必須是字串 (sampler enum)",
                        node_id=str(node_id), input_name=str(input_name),
                    )
            # MODEL fields are validated by capability check (Gate 2).
            # IMAGE/VIDEO fields are protected; enforced in remap (Gate 5).

    missing = required - seen
    if missing:
        sorted_missing = sorted(missing)
        stage, msg = template_errors.gate4_inputs_msg(sorted_missing)
        raise RunGateFailure(
            gate=4, stage=stage, msg=msg,
            audit_detail={"missing": sorted_missing},
        )


def _apply_user_inputs(
    workflow: dict[str, Any],
    *,
    analysis: WorkflowAnalysis,
    user_inputs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """§10.3.3-compliant input patcher.

    Hard-fails any patch that targets a protected media slot (those
    must come through the remap step, never through user_inputs).
    """
    for node_id, patch in (user_inputs or {}).items():
        node = workflow.get(str(node_id))
        if node is None:
            continue
        class_type = str(node.get("class_type") or "")
        node_inputs = node.get("inputs")
        if not isinstance(node_inputs, dict):
            continue
        for input_name, value in patch.items():
            if (class_type, input_name) in PROTECTED_MEDIA_INPUTS:
                raise SafetyError(
                    f"node {node_id}.{input_name} 是受保護欄位 (LoadImage / LoadImageMask / LoadVideo)，"
                    f"不允許透過 user_inputs 覆蓋；必須走 image_field_assignments"
                )
            field_obj = next(
                (
                    f
                    for f in analysis.user_inputs
                    if f.node_id == str(node_id) and f.input_name == str(input_name)
                ),
                None,
            )
            if field_obj and field_obj.category == FieldCategory.TEXT:
                value = _normalize_embedding_shortcuts(value)
            node_inputs[input_name] = value
    return workflow


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------


def run_workflow_through_gates(
    *,
    raw_workflow: Mapping[str, Any],
    user_inputs: Mapping[str, Any] | None,
    image_field_assignments: Mapping[str, str] | None,
    actor: Mapping[str, Any],
    user_id: int,
    run_id: str,
    conn,
    comfyui_client,
    upload_callback: UploadCallback,
    fetch_file_row: Callable[[Any, str], Mapping[str, Any] | None] | None = None,
    upload_scan_skip_allowed: bool = False,
    image_decoder: Callable[[bytes], None] | None = None,
    file_bytes_loader: Callable[[Mapping[str, Any]], bytes] | None = None,
) -> RunGateResult:
    """Run a workflow through all five §10 gates. Returns a patched
    workflow ready to be queued; raises RunGateFailure on any rejection.

    Caller responsibilities:
    - Re-fetch ``raw_workflow`` from DB / preset on each call (§10 Gate 1
      explicitly re-sanitizes).
    - Provide a fresh ``run_id`` (uuid hex) per call so Gate 5 can write
      to ``ComfyUI input/<run_id>/`` and §10.3.2 cleanup can target it.
    - On RunGateFailure for gate>=5, run cleanup_run_temp_files(run_id)
      since image bytes may have been uploaded mid-flight.
    - On RunGateFailure for any gate, audit a
      ``COMFYUI_TEMPLATE_RUN_GATE_FAIL`` record with the failure's stage
      and audit_detail (per §10.3.1).
    """
    # ----- Gate 1: sanitize + normalize + analyze --------------------------
    try:
        sanitized_wrapper = sanitize_workflow_json(raw_workflow)
    except WorkflowValidationError as exc:
        stage, msg = template_errors.gate1_sanitize_msg(str(exc))
        raise RunGateFailure(gate=1, stage=stage, msg=msg, audit_detail={}) from exc
    workflow = sanitized_wrapper.get("workflow_json") or {}
    try:
        analysis = analyze_workflow_json(workflow)
    except WorkflowValidationError as exc:
        stage, msg = template_errors.gate1_analyze_msg(str(exc))
        raise RunGateFailure(gate=1, stage=stage, msg=msg, audit_detail={}) from exc
    workflow = rewrite_workflow_model_inputs_to_local_options(
        workflow,
        client=comfyui_client,
    )
    analysis = analyze_workflow_json(workflow)

    # ----- Gate 2: capability ---------------------------------------------
    capability = check_workflow_capability(analysis, client=comfyui_client)
    if capability.overall == "UNSUPPORTED":
        stage, msg = template_errors.gate2_capability_msg(capability.unsupported)
        raise RunGateFailure(
            gate=2, stage=stage, msg=msg,
            audit_detail={
                "unsupported": capability.unsupported,
                "blockers": capability.blockers,
            },
        )
    # Missing model names can be fixed by user_inputs on editable model fields.
    # Defer this failure until after Gate 5 applies those overrides and rewrites
    # remote/local model aliases. Unsupported nodes still fail immediately.

    # ----- Gate 3: enforce allowlist --------------------------------------
    try:
        enforce_allowlist(analysis)
    except SafetyError as exc:
        stage, msg = template_errors.gate3_allowlist_msg(str(exc))
        raise RunGateFailure(
            gate=3, stage=stage, msg=msg,
            audit_detail={"unknown_or_denied": sorted(
                analysis.unknown_classes | analysis.denied_classes
            )},
        ) from exc

    # ----- Gate 4: required inputs + constraints --------------------------
    try:
        _check_user_input_constraints(analysis, user_inputs or {})
    except RunGateFailure:
        raise

    # ----- Gate 5: safety rewrite + image remap + apply user_inputs -------
    try:
        workflow = rewrite_save_image_prefix(workflow, user_id=int(user_id), run_id=run_id)
        workflow = _apply_user_inputs(
            workflow,
            analysis=analysis,
            user_inputs=user_inputs or {},
        )
        workflow = apply_workflow_compatibility_fixes(workflow)
        workflow = rewrite_workflow_model_inputs_to_local_options(
            workflow,
            client=comfyui_client,
        )
        # Model fields are user-editable, so the raw-workflow capability check
        # above is not enough. Re-check after applying user_inputs to catch stale
        # UI state such as a template VAELoader being overwritten with a missing
        # or sentinel VAE name before the job reaches ComfyUI.
        analysis = analyze_workflow_json(workflow)
        capability = check_workflow_capability(analysis, client=comfyui_client)
        if capability.overall == "UNSUPPORTED":
            stage, msg = template_errors.gate2_capability_msg(capability.unsupported)
            raise RunGateFailure(
                gate=2,
                stage=stage,
                msg=msg,
                audit_detail={
                    "unsupported": capability.unsupported,
                    "blockers": capability.blockers,
                    "post_user_inputs": True,
                },
            )
        if capability.missing_models:
            stage, msg = template_errors.gate2_models_msg(capability.missing_models)
            raise RunGateFailure(
                gate=2,
                stage=stage,
                msg=msg,
                audit_detail={
                    "missing_models": capability.missing_models,
                    "post_user_inputs": True,
                },
            )
        enforce_allowlist(analysis)
        if image_field_assignments:
            # Track temp dir for §10.3.2 sweeper before any upload happens —
            # this way even an upload-callback exception leaves an entry the
            # sweeper / handler can reap.
            register_run_dir(run_id=run_id, user_id=int(user_id))
        workflow = remap_load_image_to_cloud_file(
            workflow,
            image_field_assignments=dict(image_field_assignments or {}),
            actor=actor,
            conn=conn,
            run_id=run_id,
            upload_callback=upload_callback,
            fetch_file_row=fetch_file_row,
            upload_scan_skip_allowed=upload_scan_skip_allowed,
            image_decoder=image_decoder,
            file_bytes_loader=file_bytes_loader,
        )
    except SafetyError as exc:
        stage, msg = template_errors.gate5_safety_msg(str(exc))
        raise RunGateFailure(gate=5, stage=stage, msg=msg, audit_detail={}) from exc

    return RunGateResult(
        workflow=workflow,
        analysis=analysis,
        capability=capability,
        audit_metadata={
            "node_count": len(workflow),
            "overall": capability.overall,
            "image_remapped": len(image_field_assignments or {}),
        },
    )


__all__ = [
    "RunGateFailure",
    "RunGateResult",
    "run_workflow_through_gates",
]
