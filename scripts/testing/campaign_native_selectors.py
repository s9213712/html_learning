"""Pure semantic selectors for formal native scenario probe artifacts.

The selectors intentionally do not treat a process exit code, a top-level
``ok`` field, or an HTTP acceptance response as scenario evidence.  Each
boolean is derived from terminal domain state and independently observable
side effects in the source artifacts.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping

from scripts.testing.bt_formal_local_probe import (
    MANDATORY_CHECK_IDS as BT_MANDATORY_CHECK_IDS,
    PROBE_NAME as BT_PROBE_NAME,
    SCHEMA_VERSION as BT_PROBE_SCHEMA_VERSION,
    derive_checks as derive_bt_checks,
)
from services.comfyui.template.seeding import SYSTEM_WORKFLOW_IDS


FORMAL_SAFE_GGUF_ALLOWLIST = {
    ("diving_illustrious_flat_anime_sdxl", "q4_k_m"): 1_446_633_120,
    ("calcuis_illustrious_sdxl", "q4_0"): 1_457_146_848,
}
FORMAL_SAFE_GGUF_COMPANIONS = {
    ("diving_illustrious_flat_anime_sdxl", "q4_k_m"): {
        "clip_name1": ("clip_l.safetensors", 246_144_378),
        "clip_name2": ("clip_g.safetensors", 1_389_363_370),
        "vae_name": ("illustrious_vae.safetensors", 167_340_358),
    },
    ("calcuis_illustrious_sdxl", "q4_0"): {
        "clip_name1": ("illustrious_clip_l.safetensors", 247_330_924),
        "clip_name2": ("illustrious_clip_g.safetensors", 1_389_389_196),
        "vae_name": ("illustrious_vae.safetensors", 167_340_358),
    },
}
EXPECTED_COMFY_CGROUP_LIMITS = {
    "memory_high": 5 * 1024**3,
    "memory_max": 6 * 1024**3,
    "memory_swap_max": 512 * 1024**2,
    "cpu_quota": 300_000,
    "cpu_period": 100_000,
    "pids_max": 384,
}
MEDIA_BROWSER_LATENCY_SCHEMA_VERSION = "hackme.browser-video-latency/v1"
MEDIA_FIRST_FRAME_SLA_MS = 8_000.0
MEDIA_RANDOM_SEEK_SLA_MS = 5_000.0
EXPECTED_COMFY_SAFETY_FIELDS = {
    "backend.queue_running",
    "backend.queue_pending",
    "backend.queue_depth",
    "backend.system",
    "backend.devices",
    "backend.ram_total_bytes",
    "backend.ram_free_bytes",
    "backend.device_vram_total_bytes",
    "backend.device_vram_free_bytes",
    "backend.gpu_utilization_percent",
    "backend.gpu_temperature_c",
    "backend.process_pid",
    "backend.process_cgroup_path",
    "backend.process_inside_campaign_scope",
    "backend.process_listening_socket_verified",
    "backend.process_tree_pids",
    "backend.process_tree_rss_bytes",
    "backend.process_tree_threads",
    "backend.process_tree_fd_count",
    "host.loadavg",
    "host.mem_available_bytes",
    "host.swap_free_bytes",
    "host.disk_free_bytes",
    "host.psi_cpu",
    "host.psi_memory",
    "host.psi_io",
    "cgroup.path",
    "cgroup.memory_high",
    "cgroup.memory_current",
    "cgroup.memory_max",
    "cgroup.memory_swap_current",
    "cgroup.memory_swap_max",
    "cgroup.memory_events",
    "cgroup.cpu_stat",
    "cgroup.cpu_max",
    "cgroup.pids_current",
    "cgroup.pids_max",
}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _readable_regular_file(value: object) -> bool:
    path = Path(str(value or "")).expanduser()
    try:
        return bool(
            path.is_absolute()
            and path.is_file()
            and not path.is_symlink()
            and path.stat().st_size > 0
        )
    except OSError:
        return False


def _file_size_matches(value: object, expected: int) -> bool:
    path = Path(str(value or "")).expanduser()
    try:
        return _readable_regular_file(path) and path.stat().st_size == int(expected)
    except OSError:
        return False


def _image_file_decodes(path: Path) -> bool:
    try:
        from PIL import Image

        data = path.read_bytes()
        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
        return width >= 1 and height >= 1
    except Exception:
        return False


def _media_file_decodes(path: Path, expected_kind: str) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "stream=codec_type:format=format_name,duration,size",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
        streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
        return any(
            isinstance(row, Mapping) and str(row.get("codec_type") or "") == expected_kind
            for row in streams
        )
    except Exception:
        return False


def _structured_file_parses(path: Path) -> bool:
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
        elif suffix == ".jsonl":
            rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if not rows:
                return False
            for line in rows:
                if not isinstance(json.loads(line), Mapping):
                    return False
        elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            return _image_file_decodes(path)
    except Exception:
        return False
    return True


def _output_artifact_valid(row: Mapping[str, Any]) -> bool:
    path = Path(str(row.get("path") or "")).expanduser()
    if not _readable_regular_file(path):
        return False
    try:
        if int(row.get("size_bytes") or -1) != path.stat().st_size:
            return False
    except OSError:
        return False
    if str(row.get("sha256") or "") != _sha256_file(path):
        return False
    kind = str(row.get("kind") or "").lower()
    mime = str(row.get("mime_type") or "").lower()
    if kind == "image" or mime.startswith("image/"):
        return _image_file_decodes(path)
    if kind in {"video", "audio"}:
        return _media_file_decodes(path, kind)
    return False


def _safety_samples_valid(value: object, *, expected_scope: str) -> bool:
    path = Path(str(value or "")).expanduser()
    if not _readable_regular_file(path):
        return False
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception:
        return False
    if not rows:
        return False
    for row in rows:
        if not isinstance(row, Mapping):
            return False
        backend = _mapping(row.get("backend"))
        cgroup = _mapping(row.get("cgroup"))
        hard = _mapping(row.get("hard_limit_state"))
        expected = set(row.get("expected_fields") or [])
        valid = set(row.get("valid_fields") or [])
        if not (
            row.get("sample_schema_version") == "hackme.formal-comfyui-safety-sample/v1"
            and expected == EXPECTED_COMFY_SAFETY_FIELDS
            and valid == expected
            and not list(row.get("missing_fields") or [])
            and not list(row.get("collector_errors") or [])
            and hard.get("ok") is True
            and int(backend.get("process_pid") or 0) > 1
            and backend.get("process_inside_campaign_scope") is True
            and backend.get("process_listening_socket_verified") is True
            and isinstance(backend.get("process_tree_pids"), list)
            and int(backend.get("process_pid") or 0) in backend.get("process_tree_pids")
            and int(backend.get("process_tree_rss_bytes") or 0) > 0
            and int(backend.get("process_tree_rss_bytes") or 0)
            < EXPECTED_COMFY_CGROUP_LIMITS["memory_high"]
            and int(backend.get("process_tree_threads") or 0) > 0
            and int(backend.get("process_tree_fd_count") or 0) > 0
            and (
                str(backend.get("process_cgroup_path") or "") == expected_scope
                or str(backend.get("process_cgroup_path") or "").startswith(expected_scope.rstrip("/") + "/")
            )
        ):
            return False
        utilization = backend.get("gpu_utilization_percent")
        temperatures = backend.get("gpu_temperature_c")
        vram_free = backend.get("device_vram_free_bytes")
        if not (
            isinstance(utilization, list)
            and utilization
            and all(isinstance(value, int) and 0 <= value <= 100 for value in utilization)
            and isinstance(temperatures, list)
            and len(temperatures) == len(utilization)
            and all(isinstance(value, int) and 0 <= value <= 80 for value in temperatures)
            and isinstance(vram_free, list)
            and vram_free
            and all(isinstance(value, int) and value >= 256 * 1024**2 for value in vram_free)
        ):
            return False
        cpu_max = _mapping(cgroup.get("cpu_max"))
        if not (
            str(cgroup.get("path") or "") == expected_scope
            and int(cgroup.get("memory_high") or -1) == EXPECTED_COMFY_CGROUP_LIMITS["memory_high"]
            and int(cgroup.get("memory_max") or -1) == EXPECTED_COMFY_CGROUP_LIMITS["memory_max"]
            and int(cgroup.get("memory_swap_max") or -1) == EXPECTED_COMFY_CGROUP_LIMITS["memory_swap_max"]
            and int(cpu_max.get("quota") or -1) == EXPECTED_COMFY_CGROUP_LIMITS["cpu_quota"]
            and int(cpu_max.get("period") or -1) == EXPECTED_COMFY_CGROUP_LIMITS["cpu_period"]
            and int(cgroup.get("pids_max") or -1) == EXPECTED_COMFY_CGROUP_LIMITS["pids_max"]
        ):
            return False
    return True


def _workflow_input_cleanup_validation_valid(value: object) -> bool:
    row = _mapping(value)
    receipt = _mapping(row.get("receipt"))
    accepted_run_id = str(row.get("accepted_run_id") or "")
    terminal_run_id = str(row.get("terminal_run_id") or "")
    try:
        accepted_count = int(row.get("accepted_assignment_count"))
        terminal_count = int(row.get("terminal_assignment_count"))
        ref_count = int(row.get("input_ref_count"))
    except (TypeError, ValueError):
        return False
    if not (
        row.get("ok") is True
        and not list(row.get("reasons") or [])
        and receipt.get("schema_version") == 1
        and receipt.get("ok") is True
        and receipt.get("absence_verified") is True
        and terminal_run_id == accepted_run_id
        and str(receipt.get("run_id") or "") == accepted_run_id
        and terminal_count == accepted_count
        and int(receipt.get("input_ref_count") if receipt.get("input_ref_count") is not None else -1)
        == ref_count
    ):
        return False
    if accepted_count == 0:
        return bool(
            not accepted_run_id
            and not terminal_run_id
            and ref_count == 0
            and receipt.get("detail") == "no_temp_inputs"
        )
    if accepted_count < 0 or not accepted_run_id or ref_count < accepted_count:
        return False
    cleanup = _mapping(receipt.get("cleanup"))
    refs = _rows(cleanup.get("refs"))
    method = str(cleanup.get("method") or "")
    binding = _mapping(cleanup.get("local_binding"))
    if method == "local_filesystem":
        listener_pid = cleanup.get("listener_pid")
        listener_inode = str(cleanup.get("listener_inode") or "")
        listener_cwd = str(cleanup.get("listener_cwd") or "")
        project_dir = str(binding.get("project_dir") or "")
        listeners = _rows(binding.get("listeners"))
        method_valid = bool(
            cleanup.get("binding_verified") is True
            and binding.get("binding_verified") is True
            and isinstance(listener_pid, int)
            and listener_pid > 0
            and listener_inode.isdigit()
            and listener_cwd
            and listener_cwd == project_dir
            and cleanup.get("directory_absent") is True
            and any(
                item.get("pid") == listener_pid
                and str(item.get("inode") or "") == listener_inode
                and str(item.get("cwd") or "") == listener_cwd
                and item.get("cwd_matches_project") is True
                for item in listeners
            )
            and all(str(item.get("verification") or "") == "local_lstat" for item in refs)
        )
    elif method == "remote_delete_and_get":
        method_valid = bool(
            cleanup.get("binding_verified") is False
            and binding.get("binding_verified") is False
            and cleanup.get("directory_absent") is None
            and all(str(item.get("verification") or "") == "http_404" for item in refs)
        )
    else:
        method_valid = False
    return bool(
        cleanup.get("ok") is True
        and cleanup.get("absence_verified") is True
        and method_valid
        and len(refs) == ref_count
        and all(
            str(_mapping(ref.get("ref")).get("subfolder") or "") == accepted_run_id
            and str(_mapping(ref.get("ref")).get("type") or "") == "input"
            and str(_mapping(ref.get("ref")).get("filename") or "")
            and ref.get("absent") is True
            and str(ref.get("verification") or "")
            for ref in refs
        )
    )


def _final_model_safety_validation_valid(value: object) -> bool:
    row = _mapping(value)
    files = _rows(row.get("model_files"))
    graph_sha256 = str(row.get("graph_sha256") or "")
    receipt_sha256 = str(row.get("receipt_sha256") or "")
    recomputed = str(row.get("recomputed_receipt_sha256") or "")
    backend_binding = _mapping(row.get("backend_history_binding"))

    def _sha256_valid(text: str) -> bool:
        return len(text) == 64 and all(char in "0123456789abcdef" for char in text)

    def _as_int(raw: object, default: int = -1) -> int:
        try:
            return int(raw)
        except (TypeError, ValueError, OverflowError):
            return default

    if not (
        row.get("ok") is True
        and row.get("schema_version") == "hackme.comfyui-final-model-safety/v1"
        and not list(row.get("errors") or [])
        and _sha256_valid(graph_sha256)
        and _sha256_valid(receipt_sha256)
        and receipt_sha256 == recomputed
        and str(row.get("backend_origin") or "").startswith(("http://", "https://"))
        and Path(str(row.get("models_root_realpath") or "")).is_absolute()
        and _as_int(row.get("reference_count"), 0) > 0
        and _as_int(row.get("distinct_model_file_count")) == len(files)
        and len(files) > 0
        and 0 < _as_int(row.get("distinct_model_total_bytes"), 0) <= 4 * 1024**3
        and row.get("terminal_model_files_unchanged") is True
        and _as_int(row.get("terminal_model_file_revalidated_count")) == len(files)
        and row.get("backend_history_binding_verified") is True
        and backend_binding.get("schema_version")
        == "hackme.comfyui-final-model-safety-backend-binding/v1"
        and backend_binding.get("ok") is True
        and _sha256_valid(str(backend_binding.get("graph_sha256") or ""))
        and str(backend_binding.get("graph_sha256") or "") == graph_sha256
        and _sha256_valid(str(backend_binding.get("receipt_sha256") or ""))
        and str(backend_binding.get("receipt_sha256") or "") == receipt_sha256
        and str(backend_binding.get("prompt_id") or "")
        == str(row.get("terminal_prompt_id") or "")
        and backend_binding.get("history_graph_verified") is True
        and backend_binding.get("history_marker_verified") is True
        and _as_int(backend_binding.get("history_prompt_tuple_minimum_fields")) >= 4
    ):
        return False
    seen: set[str] = set()
    total = 0
    for item in files:
        relative = str(item.get("relative_path") or "")
        parts = relative.split("/")
        stat_receipt = _mapping(item.get("stat"))
        size = _as_int(item.get("size_bytes"))
        if not (
            relative
            and not relative.startswith(("/", "\\"))
            and "\\" not in relative
            and all(part not in {"", ".", ".."} for part in parts)
            and relative not in seen
            and 0 < size <= 2 * 1024**3
            and _sha256_valid(str(item.get("sha256") or ""))
            and _as_int(stat_receipt.get("size_bytes")) == size
            and _as_int(stat_receipt.get("device")) >= 0
            and _as_int(stat_receipt.get("inode")) > 0
            and _as_int(stat_receipt.get("mode")) > 0
            and _as_int(stat_receipt.get("link_count")) > 0
            and _as_int(stat_receipt.get("mtime_ns")) > 0
            and _as_int(stat_receipt.get("ctime_ns")) > 0
        ):
            return False
        seen.add(relative)
        total += size
    return total == _as_int(row.get("distinct_model_total_bytes"))


def _exact_discard_receipt_valid(value: object) -> bool:
    discard = _mapping(value)
    binding = _mapping(discard.get("local_binding"))
    verification = str(discard.get("verification") or "")
    if verification == "local_lstat_absent":
        listener_pid = binding.get("listener_pid")
        listener_inode = str(binding.get("listener_inode") or "")
        listener_cwd = str(binding.get("listener_cwd") or "")
        project_dir = str(binding.get("project_dir") or "")
        listeners = _rows(binding.get("listeners"))
        proof_valid = bool(
            binding.get("binding_verified") is True
            and isinstance(listener_pid, int)
            and listener_pid > 0
            and listener_inode.isdigit()
            and listener_cwd
            and listener_cwd == project_dir
            and any(
                item.get("pid") == listener_pid
                and str(item.get("inode") or "") == listener_inode
                and str(item.get("cwd") or "") == listener_cwd
                and item.get("cwd_matches_project") is True
                for item in listeners
            )
        )
    elif verification == "http_404":
        proof_valid = binding.get("binding_verified") is False
    else:
        proof_valid = False
    return bool(
        discard.get("absence_verified") is True
        and (
            discard.get("file_deleted") is True
            or discard.get("file_missing") is True
        )
        and discard.get("remote_preview_only") is False
        and proof_valid
    )


def _manifest_dependency_contract_valid(value: object) -> bool:
    contract = _mapping(value)
    graph = _mapping(contract.get("graph"))
    manifest = _mapping(contract.get("manifest"))
    differences = _mapping(contract.get("differences"))
    scope = _mapping(contract.get("scope"))
    custom_evidence = _mapping(contract.get("custom_node_evidence"))

    def model_rows(rows: object) -> tuple[bool, set[tuple[str, str]]]:
        raw = rows if isinstance(rows, list) else []
        parsed = {
            (str(row.get("kind") or ""), str(row.get("name") or ""))
            for row in _rows(raw)
            if str(row.get("kind") or "") and str(row.get("name") or "")
        }
        return bool(
            isinstance(rows, list)
            and len(_rows(raw)) == len(raw)
            and len(parsed) == len(raw)
        ), parsed

    def names(rows: object) -> tuple[bool, set[str]]:
        raw = rows if isinstance(rows, list) else []
        parsed = {str(item) for item in raw if isinstance(item, str) and str(item)}
        return bool(isinstance(rows, list) and len(parsed) == len(raw)), parsed

    expected_categories = {"models", "loras", "controlnets", "custom_nodes"}
    differences_exact = bool(
        set(differences) == expected_categories
        and all(
            not list(_mapping(differences.get(category)).get("missing_from_manifest") or [])
            and not list(_mapping(differences.get(category)).get("extra_in_manifest") or [])
            for category in expected_categories
        )
    )
    graph_models_valid, graph_models = model_rows(graph.get("models"))
    manifest_models_valid, manifest_models = model_rows(manifest.get("models"))
    graph_loras_valid, graph_loras = names(graph.get("loras"))
    manifest_loras_valid, manifest_loras = names(manifest.get("loras"))
    graph_controlnets_valid, graph_controlnets = names(graph.get("controlnets"))
    manifest_controlnets_valid, manifest_controlnets = names(manifest.get("controlnets"))
    graph_custom_valid, graph_custom = names(graph.get("custom_nodes"))
    manifest_custom_valid, manifest_custom = names(manifest.get("custom_nodes"))
    return bool(
        contract.get("schema_version")
        == "hackme.comfyui-manifest-dependency-contract/v1"
        and contract.get("ok") is True
        and not list(contract.get("errors") or [])
        and differences_exact
        and graph_models_valid
        and manifest_models_valid
        and graph_models == manifest_models
        and graph_loras_valid
        and manifest_loras_valid
        and graph_loras == manifest_loras
        and graph_controlnets_valid
        and manifest_controlnets_valid
        and graph_controlnets == manifest_controlnets
        and graph_custom_valid
        and manifest_custom_valid
        and graph_custom == manifest_custom
        and scope.get("model_dependencies")
        == "exact_loader_class_and_input_mapping_plus_prompt_embeddings"
        and scope.get("custom_nodes") == "explicit_class_to_package_mapping_only"
        and custom_evidence.get("scope") == "explicit_class_to_package_mapping_only"
        and str(custom_evidence.get("limitation") or "")
    )


def pointschain_hft_assertions(
    stress: Mapping[str, Any],
    dispute: Mapping[str, Any],
    frontend: Mapping[str, Any],
    cleanup: Mapping[str, Any],
) -> dict[str, Mapping[str, object]]:
    """Derive all reviewed PointsChain HFT claims from raw terminal state."""

    db_counts = _mapping(stress.get("db_counts"))
    idempotency = _mapping(stress.get("idempotency_probe"))
    overspend = _mapping(stress.get("overspend_probe"))
    finalized = _mapping(stress.get("explorer_finalized_transfers"))
    verification_response = _mapping(stress.get("verify"))
    verification = _mapping(verification_response.get("verification"))
    root_ui = _mapping(frontend.get("root"))
    member_ui = _mapping(frontend.get("member"))
    chain_ui = _mapping(root_ui.get("chain"))
    cleanup_rows = _rows(cleanup.get("records"))
    fixture_usernames = [
        str(value) for value in (stress.get("fixture_usernames") or [])
        if str(value)
    ] + [
        str(value) for value in (dispute.get("fixture_usernames") or [])
        if str(value)
    ]

    first_hash = str(idempotency.get("first_transaction_hash") or "")
    second_hash = str(idempotency.get("second_transaction_hash") or "")
    findings = stress.get("findings")
    sample_errors = stress.get("sample_errors")
    high_frequency = bool(
        int(stress.get("accounts_requested") or 0) > 1
        and int(stress.get("accounts_active") or 0)
        == int(stress.get("accounts_requested") or 0)
        and int(stress.get("direct_transfer_ops_requested") or 0) > 0
        and int(stress.get("direct_transfer_completed") or -1)
        == int(stress.get("direct_transfer_ops_requested") or 0)
        and int(stress.get("direct_transfer_errors") or 0) == 0
        and int(stress.get("transfer_ops_requested") or 0) > 0
        and int(db_counts.get("prefix_confirmed") or 0) > 0
        and isinstance(findings, list)
        and not findings
        and isinstance(sample_errors, list)
        and not sample_errors
    )
    replay_and_overspend = bool(
        first_hash
        and first_hash == second_hash
        and idempotency.get("second_created") is False
        and int(overspend.get("attempt_count") or 0) > 0
        and int(overspend.get("insufficient_balance_rejection_count") or 0) > 0
        and float(overspend.get("balance_after") or 0) >= 0
        and int(db_counts.get("duplicate_request_uuid_groups") or 0) == 0
        and int(db_counts.get("duplicate_active_wallet_address_groups") or 0) == 0
    )
    external_finality = bool(
        int(stress.get("external_transfer_count") or 0) > 0
        and int(finalized.get("remaining_pending") or 0) == 0
        and not list(finalized.get("remaining_request_uuids") or [])
        and int(db_counts.get("prefix_pending") or 0) == 0
    )
    hash_chain = bool(
        verification.get("ok") is True
        and not list(verification.get("errors") or [])
        and verification.get("financial_ok") is not False
        and int(db_counts.get("database_bytes") or 0) > 0
    )
    branch_dispute = bool(
        str(dispute.get("tx_hash") or "")
        and str(dispute.get("dispute_uuid") or "")
        and str(dispute.get("proposal_uuid") or "")
        and str(dispute.get("address_risk_proposal_uuid") or "")
        and str(dispute.get("address_freeze_proposal_uuid") or "")
        and dispute.get("review_status") == "approved"
        and dispute.get("provisional_freeze_status") == "active"
        and int(dispute.get("wrong_purpose_status") or 0) == 400
        and int(dispute.get("wrong_branch_status") or 0) == 400
        and int(dispute.get("replay_status") or 0) == 400
        and all(value is False for value in _mapping(dispute.get("redaction")).values())
    )
    desktop_mobile = bool(
        root_ui.get("ok") is True
        and member_ui.get("ok") is True
        and chain_ui.get("visible") is True
        and str(chain_ui.get("ok_text") or "").lower() in {"完整", "ok"}
        and isinstance(frontend.get("browser_errors"), list)
        and not frontend.get("browser_errors")
    )
    cleanup_complete = bool(
        fixture_usernames
        and len(cleanup_rows) == len(set(fixture_usernames))
        and {str(row.get("username") or "") for row in cleanup_rows}
        == set(fixture_usernames)
        and all(
            int(row.get("residual_exact_count") or 0) == 0
            and row.get("deleted") is True
            for row in cleanup_rows
        )
    )
    evidence = {
        "high_frequency_transfer_and_trade": high_frequency,
        "idempotency_overspend_replay_rejection": replay_and_overspend,
        "external_address_and_finality": external_finality,
        "hash_chain_verify": hash_chain,
        "branch_and_dispute_api": branch_dispute,
        "post_stress_desktop_mobile_ui": desktop_mobile,
    }
    terminal = {
        "all_domain_assertions_true": all(evidence.values()),
        "chain_has_no_pending_campaign_requests": int(db_counts.get("prefix_pending") or 0) == 0,
        "probe_findings_empty": isinstance(findings, list) and not findings,
    }
    cleanup_assertions = {
        "exact_fixture_accounts_removed": cleanup_complete,
        "no_residual_exact_usernames": cleanup_complete,
    }
    return {
        "scenario_assertions": evidence,
        "terminal_assertions": terminal,
        "cleanup_assertions": cleanup_assertions,
        "details": {
            "source_count": 4,
            "fixture_account_count": len(set(fixture_usernames)),
        },
    }


def media_long_assertions(
    stress: Mapping[str, Any],
    restart: Mapping[str, Any],
) -> dict[str, Mapping[str, object]]:
    """Derive long-video/HLS/restart claims from raw media observations."""

    phases = {
        str(row.get("phase") or ""): row
        for row in _rows(stress.get("phases"))
    }
    upload = _mapping(phases.get("upload"))
    wait = _mapping(phases.get("wait"))
    measure = _mapping(phases.get("measure"))
    share = _mapping(phases.get("share"))
    uploads = _rows(upload.get("uploads"))
    measurements = _rows(measure.get("measurements"))
    shares = _rows(share.get("shares"))
    source_media = _mapping(stress.get("source_media"))
    fixture_generation = _mapping(stress.get("fixture_generation"))

    long_fixture = bool(
        int(fixture_generation.get("requested_duration_seconds") or 0) >= 3900
        and _mapping(fixture_generation.get("media")).get("ok") is True
        and float(_mapping(fixture_generation.get("media")).get("duration_seconds") or 0) >= 3600
        and source_media.get("ok") is True
        and float(source_media.get("duration_seconds") or 0) >= 3600
    )
    starts = [int(row.get("upload_started_at_ms") or 0) for row in uploads]
    finishes = [int(row.get("upload_finished_at_ms") or 0) for row in uploads]
    parallel_upload = bool(
        len(uploads) >= 3
        and len(upload.get("accounts") or []) == len(uploads)
        and all(row.get("ok") is True and int(row.get("video_id") or 0) > 0 for row in uploads)
        and all(starts)
        and all(finishes)
        and max(starts) < min(finishes)
    )
    final_state = _mapping(wait.get("final_state"))
    jobs = _rows(final_state.get("jobs"))
    hls_terminal = bool(
        jobs
        and all(str(job.get("status") or "") == "succeeded" for job in jobs)
        and not list(wait.get("failed_jobs") or [])
    )

    variant_rows = [
        variant
        for measurement in measurements
        for variant in _rows(measurement.get("variants"))
    ]
    master_variant_segments = bool(
        measurements
        and variant_rows
        and all(
            _mapping(measurement.get("playback_json")).get("status") == 200
            and _mapping(_mapping(measurement.get("playback_json")).get("payload")).get("streaming_ready") is True
            for measurement in measurements
        )
        and all(
            _mapping(variant.get("playlist")).get("status") == 200
            and int(variant.get("media_segment_count") or 0) >= 100
            and int(_mapping(variant.get("burst")).get("requested_segments") or 0) > 0
            and int(_mapping(variant.get("burst")).get("ok_segments") or -1)
            == int(_mapping(variant.get("burst")).get("requested_segments") or 0)
            for variant in variant_rows
        )
        and all(
            _mapping(row.get("master")).get("extm3u") is True
            and _mapping(row.get("variant")).get("status") == 200
            and int(_mapping(row.get("variant")).get("sampled_segments") or 0) > 0
            and all(
                int(segment.get("status") or 0) == 200
                and int(segment.get("bytes") or 0) > 0
                for segment in _rows(row.get("segments"))
            )
            for row in shares
        )
    )
    dual_audio_subtitles = bool(
        int(source_media.get("audio_streams") or 0) >= 2
        and int(source_media.get("subtitle_streams") or 0) >= 1
        and measurements
        and all(
            len(_mapping(_mapping(row.get("playback_json")).get("payload")).get("audio_tracks") or []) >= 2
            and _rows(row.get("subtitles"))
            and all(
                subtitle.get("ok") is True
                and subtitle.get("looks_like_webvtt") is True
                for subtitle in _rows(row.get("subtitles"))
            )
            for row in measurements
        )
        and all(
            int(_mapping(row.get("playback")).get("audio_tracks") or 0) >= 2
            and int(_mapping(row.get("playback")).get("subtitles") or 0) >= 1
            for row in shares
        )
    )
    browser_rows = [
        browser
        for row in shares
        for browser in _rows(row.get("browser"))
    ]

    def browser_latency_row_passes(row: Mapping[str, Any]) -> bool:
        first_frame = _mapping(row.get("first_frame"))
        first_frame_metadata = _mapping(first_frame.get("frame_metadata"))
        seek = _mapping(row.get("seek"))
        seek_frame_metadata = _mapping(seek.get("frame_metadata"))
        layout = _mapping(row.get("layout"))
        thresholds = _mapping(row.get("latency_thresholds_ms"))
        emulation = _mapping(row.get("emulation"))
        viewport_name = str(row.get("viewport") or "")
        expected_mobile = viewport_name == "mobile"
        return bool(
            row.get("schema_version") == MEDIA_BROWSER_LATENCY_SCHEMA_VERSION
            and row.get("ok") is True
            and emulation.get("is_mobile") is expected_mobile
            and emulation.get("has_touch") is expected_mobile
            and float(thresholds.get("first_frame") or 0) == MEDIA_FIRST_FRAME_SLA_MS
            and float(thresholds.get("random_seek_terminal") or 0) == MEDIA_RANDOM_SEEK_SLA_MS
            and first_frame.get("origin") in {"share_page_navigation", "unlock_submit"}
            and first_frame.get("terminal_event") == "playing_and_video_frame"
            and first_frame.get("playing_observed") is True
            and first_frame.get("frame_observed") is True
            and first_frame.get("frame_observation_method") == "requestVideoFrameCallback"
            and 0 < float(first_frame.get("elapsed_ms") or 0) <= MEDIA_FIRST_FRAME_SLA_MS
            and 0 < float(first_frame.get("play_to_frame_latency_ms") or 0) <= MEDIA_FIRST_FRAME_SLA_MS
            and int(first_frame.get("ready_state") or 0) >= 2
            and first_frame.get("paused") is False
            and not str(first_frame.get("play_error") or "")
            and int(first_frame_metadata.get("presentedFrames") or 0) > 0
            and int(first_frame_metadata.get("width") or 0) > 0
            and int(first_frame_metadata.get("height") or 0) > 0
            and float(seek.get("duration") or 0) >= 3600
            and abs(
                float(seek.get("currentTime") or 0)
                - float(seek.get("target") or 0)
            ) < 20
            and seek.get("terminal_event") == "seeked_and_video_frame"
            and seek.get("seeked_observed") is True
            and seek.get("frame_observed") is True
            and seek.get("frame_observation_method") == "requestVideoFrameCallback"
            and seek.get("random_source") == "crypto.getRandomValues"
            and 0.15 <= float(seek.get("target_ratio") or -1) <= 0.85
            and 0 < float(seek.get("terminal_latency_ms") or 0) <= MEDIA_RANDOM_SEEK_SLA_MS
            and int(seek.get("readyState") or 0) >= 2
            and seek.get("paused") is False
            and not str(seek.get("play_error") or "")
            and int(seek_frame_metadata.get("presentedFrames") or 0) > 0
            and int(seek_frame_metadata.get("width") or 0) > 0
            and int(seek_frame_metadata.get("height") or 0) > 0
            and int(layout.get("playerWidth") or 0) > 0
            and int(layout.get("playerHeight") or 0) > 0
            and int(layout.get("scrollWidth") or 0) <= int(layout.get("viewportWidth") or 0) + 2
            and not list(row.get("fatal_errors") or [])
            and not list(row.get("console_errors") or [])
        )

    random_seek = bool(
        {(str(row.get("viewport") or "")) for row in browser_rows}
        == {"desktop", "mobile"}
        and all(browser_latency_row_passes(row) for row in browser_rows)
    )
    password_and_revoke = bool(
        shares
        and all(
            int(_mapping(row.get("locked_without_password")).get("status") or 0) in {401, 403}
            and int(_mapping(row.get("wrong_password")).get("status") or 0) in {401, 403}
            and _mapping(row.get("unlock")).get("share_session_present") is True
            and int(_mapping(row.get("revoke")).get("status") or 0) == 200
            and int(_mapping(row.get("revoke")).get("post_revoke_playback_status") or 0) in {404, 410}
            and int(_mapping(row.get("revoke")).get("post_revoke_master_status") or 0) in {404, 410}
            for row in shares
        )
    )
    restart_state = _mapping(restart.get("restart"))
    restart_stopped = _mapping(restart_state.get("stopped"))
    restart_started = _mapping(restart_state.get("started"))
    planned_restart = bool(
        restart_stopped.get("process_group_remaining") is False
        and restart_stopped.get("master_process_remaining") is False
        and restart_started.get("ready") is True
        and int(restart_started.get("new_pid") or 0) > 0
        and int(restart_started.get("new_pid") or 0)
        != int(restart_stopped.get("old_pid") or 0)
    )
    before = _mapping(restart.get("before_restart"))
    after = _mapping(restart.get("after_restart"))
    continuity = bool(
        before.get("streaming_ready") is True
        and before.get("master_extm3u") is True
        and int(before.get("variant_segment_count") or 0) > 0
        and int(before.get("sample_segment_bytes") or 0) > 0
        and after.get("streaming_ready") is True
        and after.get("master_extm3u") is True
        and int(after.get("variant_segment_count") or 0) > 0
        and int(after.get("sample_segment_bytes") or 0) > 0
        and before.get("duration_seconds") == after.get("duration_seconds")
        and before.get("variant_names") == after.get("variant_names")
    )
    cleanup = _mapping(restart.get("cleanup"))
    fixture_cleanup = bool(
        cleanup.get("continuity_share_revoked") is True
        and cleanup.get("post_revoke_denied") is True
        and int(cleanup.get("expected_video_count") or 0) >= 3
        and int(cleanup.get("deleted_video_count") or 0)
        == int(cleanup.get("expected_video_count") or 0)
        and cleanup.get("all_videos_absent") is True
    )
    evidence = {
        "long_fixture_minimum_3600_seconds": long_fixture,
        "parallel_multi_account_upload": parallel_upload,
        "hls_terminal_ready": hls_terminal,
        "master_variant_segment_measurement": master_variant_segments,
        "dual_audio_and_subtitles": dual_audio_subtitles,
        "desktop_mobile_random_seek": random_seek,
        "desktop_mobile_first_frame_and_random_seek_latency": random_seek,
        "password_wrong_password_and_revoke": password_and_revoke,
        "primary_planned_restart": planned_restart,
        "post_restart_hls_share_continuity": continuity,
    }
    return {
        "scenario_assertions": evidence,
        "terminal_assertions": {
            "all_domain_assertions_true": all(evidence.values()),
            "all_hls_jobs_succeeded": hls_terminal,
            "desktop_mobile_latency_terminal": random_seek,
            "post_restart_stream_terminal": continuity,
        },
        "cleanup_assertions": {
            "continuity_share_revoked": fixture_cleanup,
            "uploaded_video_fixtures_removed": fixture_cleanup,
        },
        "details": {
            "source_count": 2,
            "upload_count": len(uploads),
            "variant_observation_count": len(variant_rows),
            "browser_latency_schema_version": MEDIA_BROWSER_LATENCY_SCHEMA_VERSION,
            "browser_latency_thresholds_ms": {
                "first_frame": MEDIA_FIRST_FRAME_SLA_MS,
                "random_seek_terminal": MEDIA_RANDOM_SEEK_SLA_MS,
            },
            "browser_latency_observations": [
                {
                    "viewport": str(row.get("viewport") or ""),
                    "first_frame_ms": float(_mapping(row.get("first_frame")).get("elapsed_ms") or 0),
                    "random_seek_terminal_ms": float(_mapping(row.get("seek")).get("terminal_latency_ms") or 0),
                }
                for row in browser_rows
            ],
        },
    }


def bt_download_assertions(
    probe: Mapping[str, Any],
    stress: Mapping[str, Any],
    restart: Mapping[str, Any],
) -> dict[str, Mapping[str, object]]:
    """Derive BT lifecycle and same-payload HLS claims from raw artifacts.

    The BT report's top-level ``ok`` and stored check booleans are not trusted
    by themselves.  The probe checks are recomputed from ``raw`` here, every
    retained artifact is reopened and hashed, and the HLS upload source must
    resolve to the exact retained magnet download whose digest matched both
    torrent download paths.
    """

    raw = _mapping(probe.get("raw"))
    reported_checks = _mapping(probe.get("checks"))
    try:
        recomputed_checks = derive_bt_checks(dict(raw))
    except (TypeError, ValueError, OSError):
        recomputed_checks = {}

    def check_passes(check_id: str) -> bool:
        reported = _mapping(reported_checks.get(check_id))
        recomputed = _mapping(recomputed_checks.get(check_id))
        return bool(
            reported.get("check_id") == check_id
            and reported.get("mandatory") is True
            and reported.get("ok") is True
            and recomputed.get("check_id") == check_id
            and recomputed.get("mandatory") is True
            and recomputed.get("ok") is True
        )

    artifact_rows = _rows(probe.get("artifacts"))
    artifacts = {
        str(row.get("artifact_id") or ""): row
        for row in artifact_rows
        if str(row.get("artifact_id") or "")
    }
    required_artifacts = {
        "source_video",
        "torrent_metainfo",
        "magnet_download",
        "torrent_file_download",
        "event_trace",
    }

    def artifact_valid(artifact_id: str) -> bool:
        row = _mapping(artifacts.get(artifact_id))
        path = Path(str(row.get("path") or "")).expanduser()
        try:
            return bool(
                row.get("exists") is True
                and row.get("validated") is True
                and path.is_absolute()
                and path.is_file()
                and not path.is_symlink()
                and int(row.get("size_bytes") or 0) == path.stat().st_size
                and str(row.get("sha256") or "") == _sha256_file(path)
            )
        except (OSError, TypeError, ValueError):
            return False

    artifact_index_valid = bool(
        required_artifacts.issubset(artifacts)
        and all(artifact_valid(artifact_id) for artifact_id in required_artifacts)
    )
    payload = _mapping(raw.get("payload"))
    magnet = _mapping(raw.get("magnet"))
    torrent_file = _mapping(raw.get("torrent_file"))
    source_path = Path(str(payload.get("source_path") or "")).expanduser()
    magnet_path = Path(str(magnet.get("download_path") or "")).expanduser()
    torrent_path = Path(str(torrent_file.get("download_path") or "")).expanduser()
    expected_digest = str(payload.get("source_sha256") or "")
    try:
        reopened_hashes_match = bool(
            len(expected_digest) == 64
            and all(_readable_regular_file(path) for path in (source_path, magnet_path, torrent_path))
            and _sha256_file(source_path) == expected_digest
            and _sha256_file(magnet_path) == expected_digest
            and _sha256_file(torrent_path) == expected_digest
            and int(payload.get("size_bytes") or 0) == source_path.stat().st_size
            and magnet_path.stat().st_size == source_path.stat().st_size
            and torrent_path.stat().st_size == source_path.stat().st_size
        )
    except (OSError, TypeError, ValueError):
        reopened_hashes_match = False

    probe_identity = bool(
        probe.get("schema_version") == BT_PROBE_SCHEMA_VERSION
        and probe.get("probe") == BT_PROBE_NAME
        and probe.get("terminal_state") == "success"
        and probe.get("ok") is True
        and probe.get("local_only") is True
        and probe.get("tracker_host_local_only") is True
        and isinstance(probe.get("errors"), list)
        and not probe.get("errors")
        and set(reported_checks) == set(BT_MANDATORY_CHECK_IDS)
        and set(recomputed_checks) == set(BT_MANDATORY_CHECK_IDS)
    )
    all_bt_checks = bool(
        probe_identity
        and artifact_index_valid
        and all(check_passes(check_id) for check_id in BT_MANDATORY_CHECK_IDS)
    )

    phases = {
        str(row.get("phase") or ""): row
        for row in _rows(stress.get("phases"))
    }
    upload = _mapping(phases.get("upload"))
    wait = _mapping(phases.get("wait"))
    measure = _mapping(phases.get("measure"))
    share = _mapping(phases.get("share"))
    uploads = _rows(upload.get("uploads"))
    measurements = _rows(measure.get("measurements"))
    shares = _rows(share.get("shares"))
    source_media = _mapping(stress.get("source_media"))
    source_checks = _mapping(stress.get("source_checks"))

    def same_path(left: object, right: Path) -> bool:
        candidate = Path(str(left or "")).expanduser()
        try:
            return bool(
                candidate.is_absolute()
                and right.is_absolute()
                and candidate.resolve(strict=True) == right.resolve(strict=True)
            )
        except OSError:
            return False

    same_download_uploaded = bool(
        reopened_hashes_match
        and same_path(source_media.get("path"), magnet_path)
        and same_path(upload.get("video"), magnet_path)
        and source_media.get("ok") is True
        and int(source_media.get("video_streams") or 0) >= 1
        and float(source_media.get("duration_seconds") or 0) > 0
        and int(source_media.get("size_bytes") or 0) == magnet_path.stat().st_size
        and int(upload.get("video_size_bytes") or 0) == magnet_path.stat().st_size
        and source_checks
        and all(value is True for value in source_checks.values())
    ) if _readable_regular_file(magnet_path) else False
    upload_terminal = bool(
        upload.get("ok") is True
        and uploads
        and all(
            row.get("ok") is True
            and int(row.get("video_id") or 0) > 0
            and str(row.get("username") or "")
            for row in uploads
        )
    )
    final_state = _mapping(wait.get("final_state"))
    jobs = _rows(final_state.get("jobs"))
    hls_jobs_terminal = bool(
        wait.get("ok") is True
        and jobs
        and all(str(job.get("status") or "") == "succeeded" for job in jobs)
        and not list(wait.get("failed_jobs") or [])
    )
    variant_rows = [
        variant
        for measurement in measurements
        for variant in _rows(measurement.get("variants"))
    ]
    hls_measured = bool(
        measure.get("ok") is True
        and measurements
        and variant_rows
        and all(
            _mapping(measurement.get("playback_json")).get("status") == 200
            and _mapping(_mapping(measurement.get("playback_json")).get("payload")).get("streaming_ready") is True
            for measurement in measurements
        )
        and all(
            int(_mapping(variant.get("playlist")).get("status") or 0) == 200
            and int(variant.get("media_segment_count") or 0) > 0
            and int(_mapping(variant.get("burst")).get("requested_segments") or 0) > 0
            and int(_mapping(variant.get("burst")).get("ok_segments") or -1)
            == int(_mapping(variant.get("burst")).get("requested_segments") or 0)
            for variant in variant_rows
        )
    )
    share_hls = bool(
        share.get("ok") is True
        and shares
        and all(
            row.get("ok") is True
            and int(_mapping(row.get("locked_without_password")).get("status") or 0) in {401, 403}
            and int(_mapping(row.get("wrong_password")).get("status") or 0) in {401, 403}
            and _mapping(row.get("unlock")).get("share_session_present") is True
            and int(_mapping(row.get("playback")).get("status") or 0) == 200
            and _mapping(row.get("playback")).get("mode") == "hls"
            and _mapping(row.get("playback")).get("streaming_ready") is True
            and _mapping(row.get("master")).get("ok") is True
            and _mapping(row.get("master")).get("extm3u") is True
            and int(_mapping(row.get("variant")).get("status") or 0) == 200
            and _mapping(row.get("variant")).get("ok") is True
            and _rows(row.get("segments"))
            and all(
                int(segment.get("status") or 0) == 200
                and int(segment.get("bytes") or 0) > 0
                for segment in _rows(row.get("segments"))
            )
            for row in shares
        )
    )
    stress_share_revoked = bool(
        shares
        and all(
            int(_mapping(row.get("revoke")).get("status") or 0) == 200
            and int(_mapping(row.get("revoke")).get("post_revoke_playback_status") or 0) in {404, 410}
            and int(_mapping(row.get("revoke")).get("post_revoke_master_status") or 0) in {404, 410}
            for row in shares
        )
    )

    restart_state = _mapping(restart.get("restart"))
    stopped = _mapping(restart_state.get("stopped"))
    started = _mapping(restart_state.get("started"))
    primary_restarted = bool(
        stopped.get("process_group_remaining") is False
        and stopped.get("master_process_remaining") is False
        and int(stopped.get("old_pid") or 0) > 0
        and started.get("ready") is True
        and int(started.get("new_pid") or 0) > 0
        and int(started.get("new_pid") or 0) != int(stopped.get("old_pid") or 0)
    )
    before = _mapping(restart.get("before_restart"))
    after = _mapping(restart.get("after_restart"))
    post_restart_hls = bool(
        restart.get("share_created") is True
        and all(
            observation.get("streaming_ready") is True
            and int(observation.get("playback_status") or 0) == 200
            and observation.get("master_extm3u") is True
            and int(observation.get("master_status") or 0) == 200
            and int(observation.get("variant_status") or 0) == 200
            and int(observation.get("variant_segment_count") or 0) > 0
            and int(observation.get("sample_segment_status") or 0) == 200
            and int(observation.get("sample_segment_bytes") or 0) > 0
            for observation in (before, after)
        )
        and before.get("duration_seconds") == after.get("duration_seconds")
        and before.get("variant_names") == after.get("variant_names")
    )
    restart_cleanup = _mapping(restart.get("cleanup"))
    media_cleanup = bool(
        stress_share_revoked
        and restart_cleanup.get("continuity_share_revoked") is True
        and restart_cleanup.get("post_revoke_denied") is True
        and int(restart_cleanup.get("expected_video_count") or 0) == len(uploads)
        and int(restart_cleanup.get("deleted_video_count") or -1) == len(uploads)
        and restart_cleanup.get("all_videos_absent") is True
    )

    evidence = {
        "controlled_local_seed": check_passes("controlled_local_seed"),
        "magnet_terminal_success": check_passes("magnet_terminal_success"),
        "torrent_file_terminal_success": check_passes("torrent_file_terminal_success"),
        "download_content_hash": bool(
            check_passes("payload_sha256_exact") and reopened_hashes_match
        ),
        "pause_resume_progress": check_passes("pause_resume_progress"),
        "service_restart_resume": check_passes("bt_client_service_restart_resume"),
        "downloaded_video_preview_share_stream_hls": bool(
            check_passes("downloaded_video_parseable")
            and same_download_uploaded
            and upload_terminal
            and hls_jobs_terminal
            and hls_measured
            and share_hls
            and primary_restarted
            and post_restart_hls
        ),
    }
    return {
        "scenario_assertions": evidence,
        "terminal_assertions": {
            "bt_machine_report_terminal_success": all_bt_checks,
            "same_download_hls_pipeline_terminal_success": bool(
                stress.get("ok") is True
                and stress.get("verdict") == "PASS"
                and same_download_uploaded
                and upload_terminal
                and hls_jobs_terminal
                and hls_measured
                and share_hls
            ),
            "post_primary_restart_hls_terminal_success": bool(
                primary_restarted and post_restart_hls
            ),
        },
        "cleanup_assertions": {
            "bt_process_ports_and_runtime_removed": check_passes(
                "precise_process_and_fixture_cleanup"
            ),
            "shares_revoked_and_uploaded_videos_removed": media_cleanup,
        },
        "details": {
            "source_count": 3,
            "bt_artifact_count": len(artifact_rows),
            "hls_upload_count": len(uploads),
            "hls_variant_observation_count": len(variant_rows),
        },
    }


def media_proxy_assertions(
    service: Mapping[str, Any],
    http: Mapping[str, Any],
    browser: Mapping[str, Any],
    chat: Mapping[str, Any],
) -> dict[str, Mapping[str, object]]:
    """Derive cross-browser proxy claims from stream and UI observations."""

    first_stream = _mapping(service.get("first_stream"))
    reopen = _mapping(service.get("reopen_stream"))
    first_metrics = _mapping(first_stream.get("metrics"))
    reopen_metrics = _mapping(reopen.get("metrics"))
    service_cleanup = _mapping(service.get("cleanup"))
    busy_recovery = bool(
        int(first_stream.get("active_after_first_open") or 0) == 1
        and str(first_stream.get("busy_error") or "").startswith("realtime_proxy_busy:")
        and int(first_stream.get("first_chunk_bytes") or 0) > 0
        and first_stream.get("active_after_close") == 0
        and first_metrics.get("closed_by_client") is True
        and int(reopen.get("output_bytes") or 0) > 1024
        and reopen.get("selected_audio") == "audio_02_eng"
        and reopen.get("active_after_reopen") == 0
        and first_metrics.get("runtime_scope") == "global"
        and reopen_metrics.get("runtime_scope") == "global"
    )

    http_phase = _mapping(http.get("http_phase"))
    held = _mapping(http_phase.get("held_standard"))
    busy = _mapping(http_phase.get("busy_standard"))
    direct = _mapping(http_phase.get("basic_direct"))
    hls = _mapping(http_phase.get("premium_hls"))
    hls_master = _mapping(hls.get("master"))
    hls_playlist = _mapping(hls.get("playlist"))
    hls_segment = _mapping(hls.get("segment"))
    held_chunk = _mapping(http_phase.get("held_standard_first_chunk"))
    server_metrics = _mapping(http.get("server_metrics_summary"))
    latest_metric = _mapping(server_metrics.get("latest"))
    metric_payload = _mapping(latest_metric.get("metrics"))
    runtime_metric = _mapping(latest_metric.get("runtime"))
    http_cleanup = _mapping(http.get("cleanup"))
    http_concurrency = bool(
        int(held.get("status") or 0) == 200
        and int(busy.get("status") or 0) == 429
        and "realtime_proxy_busy" in str(busy.get("body_sample") or "")
        and int(direct.get("status") or 0) in {200, 206}
        and int(direct.get("first_chunk_bytes") or 0) > 0
        and int(hls_master.get("status") or 0) == 200
        and int(hls_playlist.get("status") or 0) == 200
        and int(hls_segment.get("status") or 0) in {200, 206}
        and int(hls_segment.get("first_chunk_bytes") or 0) > 0
        and int(held_chunk.get("first_chunk_bytes") or 0) > 0
        and int(server_metrics.get("count") or 0) > 0
        and metric_payload.get("finished") is True
        and int(metric_payload.get("bytes_sent") or 0) > 0
        and metric_payload.get("closed_by_client") is True
        and runtime_metric.get("scope") == "global"
        and metric_payload.get("runtime_scope") == "global"
    )

    coverage = _mapping(browser.get("browser_coverage"))
    checks = _rows(browser.get("checks"))
    expected_pairs = {
        (name, viewport)
        for name in ("chromium", "firefox", "webkit")
        for viewport in ("desktop", "mobile")
    }
    observed_pairs = {
        (str(row.get("browser") or ""), str(row.get("viewport") or ""))
        for row in checks
    }
    browser_complete = bool(
        browser.get("require_all_browsers") is True
        and coverage.get("mode") == "require_all_browsers"
        and int(coverage.get("expected_check_count") or 0) == 6
        and int(coverage.get("observed_check_count") or 0) == 6
        and not list(coverage.get("missing") or [])
        and not list(coverage.get("skipped") or [])
        and not list(coverage.get("failed") or [])
        and observed_pairs == expected_pairs
        and all(row.get("ok") is True for row in checks)
    )
    audio_subtitle = bool(
        checks
        and all(
            _mapping(row.get("standard")).get("audio_switched") is True
            and _mapping(row.get("subtitle_shift")).get("ok") is True
            and _mapping(row.get("subtitle_shift")).get("reset") is True
            and int(_mapping(row.get("subtitle_shift")).get("expected_subtitle_count") or 0) > 0
            for row in checks
        )
    )
    chat_cleanup = _mapping(chat.get("cleanup"))
    chat_embed = bool(
        "/shared/videos/probeToken_ABC-123" in str(chat.get("href") or "")
        and "#vk=probe-fragment_456" in str(chat.get("href") or "")
        and isinstance(chat.get("browser_errors"), list)
        and not chat.get("browser_errors")
        and all(
            chat_cleanup.get(key) is True
            for key in ("message_deleted", "room_deleted", "room_absent", "setting_restored")
        )
    )
    evidence = {
        "realtime_proxy_busy_disconnect_recovery": busy_recovery,
        "http_concurrency_and_backpressure": http_concurrency,
        "chromium_firefox_webkit_desktop_mobile": browser_complete,
        "audio_track_and_subtitle_switch": audio_subtitle,
        "chat_video_share_embed": chat_embed,
    }
    service_clean = all(
        service_cleanup.get(key) is True
        for key in ("active_slots_released", "held_slots_released", "environment_restored")
    )
    subprocesses_clean = bool(
        http_cleanup.get("server_stopped") is True
        and _mapping(browser.get("cleanup")).get("server_stopped") is True
    )
    return {
        "scenario_assertions": evidence,
        "terminal_assertions": {
            "all_domain_assertions_true": all(evidence.values()),
            "all_browser_checks_completed": browser_complete,
        },
        "cleanup_assertions": {
            "proxy_slots_and_environment_restored": service_clean,
            "probe_servers_stopped": subprocesses_clean,
            "chat_fixture_and_setting_removed": chat_embed,
        },
        "details": {
            "source_count": 4,
            "browser_observation_count": len(checks),
        },
    }


def final_ui_assertions(
    ui: Mapping[str, Any],
    launch_gate: Mapping[str, Any],
    invariants: Mapping[str, Any],
    load_context: Mapping[str, Any],
    process_cleanup: Mapping[str, Any],
) -> dict[str, Mapping[str, object]]:
    """Derive final desktop/mobile UX and launch claims from raw observations."""

    roles = _rows(ui.get("roles"))
    root_role = next(
        (row for row in roles if row.get("role") == "root_desktop"),
        {},
    )
    member_role = next(
        (row for row in roles if row.get("role") == "member_mobile"),
        {},
    )
    all_modules = [
        module
        for role in roles
        for module in _rows(role.get("modules"))
    ]
    member_modules = _rows(_mapping(member_role).get("modules"))
    screenshots = _rows(ui.get("screenshots"))

    member_behavior = bool(
        _mapping(member_role).get("passed") is True
        and _mapping(member_role).get("identity_role") in {"user", "member"}
        and len(_mapping(member_role).get("visible_modules") or []) >= 4
        and member_modules
        and all(module.get("passed") is True for module in member_modules)
    )
    navigation_under_load = bool(
        len(roles) == 2
        and _mapping(root_role).get("passed") is True
        and _mapping(member_role).get("passed") is True
        and len(_mapping(root_role).get("visible_modules") or []) >= 8
        and all_modules
        and all(module.get("passed") is True for module in all_modules)
        and load_context.get("campaign_active") is True
        and load_context.get("core_load_process_alive") is True
        and load_context.get("resource_monitor_alive") is True
        and load_context.get("latest_target_sample_at_load") is True
    )
    touch_targets = bool(
        member_modules
        and all(
            not list(_mapping(module.get("observation")).get("undersized") or [])
            for module in member_modules
        )
    )
    layout_clean = bool(
        all_modules
        and all(
            int(_mapping(module.get("observation")).get("rootOverflowPx") or 0) <= 6
            and not list(_mapping(module.get("observation")).get("outside") or [])
            and not list(_mapping(module.get("observation")).get("clipped") or [])
            and not list(_mapping(module.get("observation")).get("hiddenFocusable") or [])
            for module in all_modules
        )
    )
    no_silent_failure = bool(
        roles
        and all(
            not list(role.get("browser_errors") or [])
            and not list(role.get("failed_responses") or [])
            and not list(role.get("failed_requests") or [])
            for role in roles
        )
        and all(
            not list(_mapping(module.get("observation")).get("frontendFailures") or [])
            for module in all_modules
        )
    )
    screenshot_evidence = bool(
        len(screenshots) >= 4
        and {str(row.get("role") or "") for row in screenshots}
        == {"root_desktop", "member_mobile"}
        and all(
            str(row.get("path") or "").endswith(".png")
            and int(row.get("size_bytes") or 0) > 0
            and int(_mapping(row.get("viewport")).get("width") or 0) > 0
            for row in screenshots
        )
    )

    gate_summary = _mapping(launch_gate.get("WHOLE_SITE_PRODUCTION_GATE_SUMMARY"))
    gate_modules = _rows(launch_gate.get("modules"))
    launch_ready = bool(
        gate_summary.get("result") == "PASS"
        and gate_summary.get("production_readiness") == "YES"
        and int(gate_summary.get("modules_failed") or 0) == 0
        and int(gate_summary.get("critical_findings") or 0) == 0
        and int(gate_summary.get("high_findings") or 0) == 0
        and not list(gate_summary.get("unresolved_risks") or [])
        and not list(gate_summary.get("required_followups") or [])
        and gate_modules
        and all(module.get("status") == "PASS" for module in gate_modules)
    )

    readiness = _mapping(invariants.get("readiness"))
    audit = _mapping(_mapping(invariants.get("audit_integrity")).get("body"))
    audit_state = _mapping(audit.get("audit_integrity"))
    logs = _mapping(_mapping(invariants.get("mode_log_chain")).get("body"))
    database = _mapping(_mapping(invariants.get("database_integrity")).get("body"))
    database_state = _mapping(database.get("database"))
    points_job = _mapping(invariants.get("points_verify_job"))
    points_latest = _mapping(_mapping(invariants.get("points_verify_latest")).get("body"))
    points_verification = _mapping(points_latest.get("verification"))
    trading_job = _mapping(invariants.get("trading_verify_job"))
    trading_latest = _mapping(_mapping(invariants.get("trading_verify_latest")).get("body"))
    trading_verification = _mapping(trading_latest.get("verification"))
    sqlite_checks = _mapping(invariants.get("sqlite_quick_checks"))
    final_invariants = bool(
        readiness.get("ok") is True
        and audit_state.get("enabled") is True
        and audit_state.get("ok") is True
        and audit_state.get("broken_at") is None
        and int(logs.get("broken_links") or 0) == 0
        and not list(logs.get("invalid_signatures") or [])
        and logs.get("result") == "PASS"
        and database_state.get("ok") is True
        and points_job.get("terminal_status") == "succeeded"
        and points_verification.get("ok") is True
        and not list(points_verification.get("errors") or [])
        and points_verification.get("financial_ok") is not False
        and trading_job.get("terminal_status") == "succeeded"
        and trading_verification.get("ok") is True
        and not list(trading_verification.get("errors") or [])
        and sqlite_checks
        and all(_mapping(row).get("ok") is True for row in sqlite_checks.values())
    )
    evidence = {
        "heuristic_member_behavior": member_behavior,
        "all_feature_navigation_under_load": navigation_under_load,
        "critical_touch_targets_minimum_44px": touch_targets,
        "no_clipping_overflow_or_hidden_cta": layout_clean,
        "no_console_network_or_silent_failure": no_silent_failure,
        "representative_desktop_mobile_screenshots": screenshot_evidence,
        "whole_site_launch_gate": launch_ready,
        "final_db_log_chain_finance_and_pointschain_invariants": final_invariants,
    }
    browser_cleanup = bool(
        ui.get("browser_closed") is True
        and roles
        and all(role.get("context_closed") is True for role in roles)
    )
    no_orphans = bool(
        isinstance(process_cleanup.get("new_descendant_pids"), list)
        and not process_cleanup.get("new_descendant_pids")
    )
    return {
        "scenario_assertions": evidence,
        "terminal_assertions": {
            "all_domain_assertions_true": all(evidence.values()),
            "ui_sweep_terminal": ui.get("terminal_pass") is True,
            "launch_gate_terminal": launch_ready,
        },
        "cleanup_assertions": {
            "browser_and_contexts_closed": browser_cleanup,
            "no_new_descendant_processes": no_orphans,
            "read_only_ui_sweep_has_no_fixture_inventory": not list(ui.get("fixture_inventory") or []),
        },
        "details": {
            "source_count": 5,
            "module_observation_count": len(all_modules),
            "screenshot_count": len(screenshots),
        },
    }


def wallet_incident_assertions(
    realistic: Mapping[str, Any],
    replay: Mapping[str, Any],
    branch: Mapping[str, Any],
    final_state: Mapping[str, Any],
    cleanup: Mapping[str, Any],
) -> dict[str, Mapping[str, object]]:
    """Derive wallet-compromise and governed recovery claims from live state."""

    incident = _mapping(realistic.get("incident"))
    users = _mapping(realistic.get("users"))
    proposals = _mapping(realistic.get("proposals"))
    attacker_state = _mapping(realistic.get("attacker_wallet_state"))
    risk_label = _mapping(attacker_state.get("risk_label"))
    governance_freeze = _mapping(attacker_state.get("governance_freeze"))
    balances = _mapping(realistic.get("balances"))
    after_recovery = _mapping(balances.get("after_recovery"))
    realistic_dispute = _mapping(realistic.get("dispute"))
    replay_redaction = _mapping(replay.get("redaction"))
    final_job = _mapping(final_state.get("points_verify_job"))
    final_latest = _mapping(_mapping(final_state.get("points_verify_latest")).get("body"))
    final_verification = _mapping(final_latest.get("verification"))
    cleanup_rows = _rows(cleanup.get("records"))
    fixture_usernames = sorted({
        str(_mapping(row).get("username") or "")
        for row in users.values()
        if str(_mapping(row).get("username") or "")
    } | {
        str(value) for value in (replay.get("fixture_usernames") or []) if str(value)
    })

    compromise = bool(
        str(incident.get("theft_tx_hash") or "")
        and str(incident.get("attacker_spend_tx_hash") or "")
        and int(incident.get("claimed_amount") or 0) > 0
        and _mapping(users.get("victim")).get("wallet")
        and _mapping(users.get("attacker")).get("wallet")
    )
    replay_rejection = bool(
        int(replay.get("replay_status") or 0) == 400
        and int(replay.get("wrong_purpose_status") or 0) == 400
        and int(replay.get("wrong_branch_status") or 0) == 400
        and replay_redaction
        and all(value is False for value in replay_redaction.values())
    )
    freeze_and_risk = bool(
        realistic.get("blocked_after_freeze") is True
        and str(realistic.get("blocked_after_freeze_reason") or "")
        and risk_label.get("status") == "active"
        and governance_freeze.get("freeze_type") == "governance"
    )
    proposal_rows = [_mapping(proposals.get(key)) for key in (
        "recovery", "address_risk", "address_freeze"
    )]
    dispute_and_votes = bool(
        realistic_dispute.get("status") == "approved"
        and all(str(row.get("proposal_uuid") or "") for row in proposal_rows)
        and all(str(row.get("vote_status") or "") in {"passed", "executed"} for row in proposal_rows)
        and all(isinstance(row.get("execution"), Mapping) for row in proposal_rows)
    )
    append_only_compensation = bool(
        int(balances.get("victim_recovered_points") or 0) > 0
        and int(after_recovery.get("victim") or 0) > 0
        and final_job.get("terminal_status") == "succeeded"
        and final_verification.get("ok") is True
        and not list(final_verification.get("errors") or [])
        and final_verification.get("financial_ok") is not False
        and _mapping(final_state.get("theft_explorer")).get("status") == 200
    )
    governed_branch = bool(
        str(branch.get("proposal_uuid") or "")
        and str(branch.get("branch_uuid") or "")
        and str(branch.get("parent_branch_uuid") or "")
        and isinstance(branch.get("recovery_seed"), Mapping)
        and branch.get("execution_action") == "canonical_recovery_branch_activated"
        and str(branch.get("root_vote_status") or "") in {"open", "passed", "executed"}
        and str(branch.get("manager_vote_status") or "") in {"passed", "executed"}
    )
    evidence = {
        "simulated_key_compromise_and_theft": compromise,
        "double_spend_and_replay_rejection": replay_rejection,
        "wallet_freeze_and_risk_marker": freeze_and_risk,
        "public_dispute_and_governance_votes": dispute_and_votes,
        "append_only_compensation": append_only_compensation,
        "governed_recovery_branch": governed_branch,
    }
    cleanup_complete = bool(
        fixture_usernames
        and cleanup.get("login_succeeded") is True
        and {str(row.get("username") or "") for row in cleanup_rows}
        == set(fixture_usernames)
        and all(
            row.get("deleted") is True
            and int(row.get("residual_exact_count") or 0) == 0
            for row in cleanup_rows
        )
    )
    return {
        "scenario_assertions": evidence,
        "terminal_assertions": {
            "all_domain_assertions_true": all(evidence.values()),
            "recovery_target_ready": _mapping(final_state.get("readiness")).get("ok") is True,
            "pointschain_verify_succeeded": append_only_compensation,
        },
        "cleanup_assertions": {
            "exact_fixture_accounts_removed": cleanup_complete,
            "no_residual_exact_usernames": cleanup_complete,
        },
        "details": {
            "source_count": 5,
            "fixture_account_count": len(fixture_usernames),
        },
    }


def backup_restore_assertions(
    portable: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    live_restore: Mapping[str, Any],
    restart: Mapping[str, Any],
    final_state: Mapping[str, Any],
    cleanup: Mapping[str, Any],
) -> dict[str, Mapping[str, object]]:
    """Derive snapshot/full-runtime/restore claims from independently read state."""

    archive = _mapping(portable.get("archive"))
    extracted = _mapping(portable.get("extracted_restore"))
    portable_sqlite = _mapping(extracted.get("sqlite_quick_checks"))
    live_sqlite = _mapping(live_restore.get("sqlite_quick_checks"))
    policy = _mapping(live_restore.get("restore_policy"))
    stop_state = _mapping(restart.get("stopped"))
    start_state = _mapping(restart.get("started"))
    final_job = _mapping(final_state.get("points_verify_job"))
    latest = _mapping(_mapping(final_state.get("points_verify_latest")).get("body"))
    verification = _mapping(latest.get("verification"))

    snapshot_boundary = bool(
        snapshot.get("snapshot_status") in {200, 201}
        and snapshot.get("snapshot_id_present") is True
        and snapshot.get("dirty_marker_created") is True
        and snapshot.get("dirty_marker_absent_after_restore") is True
        and snapshot.get("transfer_survived_restore") is True
        and snapshot.get("storage_restored") is True
        and _mapping(snapshot.get("protected_database_skips")).get("finance")
        == "append_only_financial_restore_disabled"
    )
    portable_archive = bool(
        archive.get("readable") is True
        and int(archive.get("size_bytes") or 0) > 0
        and len(str(archive.get("sha256") or "")) == 64
        and int(archive.get("manifest_file_count") or 0) > 0
        and int(archive.get("archive_regular_file_count") or 0)
        == int(archive.get("manifest_file_count") or 0) + 1
        and int(archive.get("database_file_count") or 0) > 0
        and int(archive.get("storage_file_count") or 0) > 0
        and not list(archive.get("unsafe_members") or [])
        and not list(extracted.get("hash_mismatches") or [])
        and extracted.get("all_manifest_files_present") is True
    )
    live_protection = bool(
        int(live_restore.get("backup_command_returncode", -1)) == 0
        and int(live_restore.get("restore_command_returncode", -1)) == 0
        and int(live_restore.get("archive_size_bytes") or 0) > 0
        and live_restore.get("archive_readable") is True
        and live_restore.get("protected_finance_hash_preserved") is True
        and live_restore.get("storage_preserved") is True
        and policy.get("policy") == "append_only_financial_restore_disabled"
        and live_restore.get("dirty_marker_absent_after_restore") is True
        and live_restore.get("transfer_survived_restore") is True
    )
    sqlite_all = bool(
        portable_sqlite
        and live_sqlite
        and all(_mapping(row).get("ok") is True for row in portable_sqlite.values())
        and all(_mapping(row).get("ok") is True for row in live_sqlite.values())
    )
    planned_restart = bool(
        int(stop_state.get("old_pid") or 0) > 0
        and stop_state.get("master_process_remaining") is False
        and stop_state.get("process_group_remaining") is False
        and int(start_state.get("new_pid") or 0) > 0
        and int(start_state.get("new_pid") or 0) != int(stop_state.get("old_pid") or 0)
        and start_state.get("readiness_succeeded") is True
    )
    post_restart = bool(
        _mapping(final_state.get("readiness")).get("ok") is True
        and final_job.get("terminal_status") == "succeeded"
        and verification.get("ok") is True
        and not list(verification.get("errors") or [])
        and verification.get("financial_ok") is not False
        and _mapping(final_state.get("snapshot_transfer_explorer")).get("status") == 200
        and _mapping(final_state.get("cli_transfer_explorer")).get("status") == 200
    )
    evidence = {
        "server_snapshot_restore_boundary": snapshot_boundary,
        "portable_full_runtime_archive": portable_archive,
        "storage_restore_and_live_finance_protection": live_protection,
        "sqlite_quick_check_all_databases": sqlite_all,
        "planned_restart_outage_and_readiness": planned_restart,
        "post_restart_state_and_chain_invariants": post_restart,
    }
    cleanup_complete = bool(
        portable.get("restore_root_removed") is True
        and live_restore.get("pre_restore_runtime_removed") is True
        and live_restore.get("storage_marker_removed") is True
        and cleanup.get("snapshot_deleted") is True
        and cleanup.get("snapshot_absent") is True
        and cleanup.get("unexpected_pre_restore_paths") == []
    )
    return {
        "scenario_assertions": evidence,
        "terminal_assertions": {
            "all_domain_assertions_true": all(evidence.values()),
            "recovery_target_ready": _mapping(final_state.get("readiness")).get("ok") is True,
            "pointschain_verify_succeeded": post_restart,
        },
        "cleanup_assertions": {
            "temporary_restore_tree_removed": cleanup_complete,
            "snapshot_and_pre_restore_fixtures_removed": cleanup_complete,
        },
        "details": {
            "source_count": 6,
            "portable_manifest_file_count": int(archive.get("manifest_file_count") or 0),
        },
    }


def server_emergency_assertions(
    enter: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
    restore: Mapping[str, Any],
    final_state: Mapping[str, Any],
) -> dict[str, Mapping[str, object]]:
    """Derive the incident containment/repair/restore lifecycle from API state."""

    entered = _mapping(enter.get("enter"))
    entered_body = _mapping(entered.get("body"))
    status_during = _mapping(_mapping(enter.get("status_during")).get("body"))
    incident_during = _mapping(status_during.get("incident"))
    mode_during = _mapping(status_during.get("mode"))
    root_restricted = _mapping(enter.get("root_restricted_operation"))
    member_restricted = _mapping(enter.get("member_restricted_operation"))
    root_recovery = _mapping(enter.get("root_recovery_operation"))

    repair = _mapping(diagnostics.get("integrity_repair"))
    repair_body = _mapping(repair.get("body"))
    audit_after_repair = _mapping(_mapping(repair_body.get("audit")).get("after"))
    diagnostic_audit = _mapping(_mapping(diagnostics.get("audit_after")).get("body"))
    diagnostic_audit_state = _mapping(diagnostic_audit.get("audit_integrity"))
    diagnostic_db = _mapping(_mapping(diagnostics.get("database_after")).get("body"))
    diagnostic_db_state = _mapping(diagnostic_db.get("database"))
    diagnostic_logs = _mapping(_mapping(diagnostics.get("mode_log_after")).get("body"))

    resolve = _mapping(restore.get("resolve"))
    resolve_body = _mapping(resolve.get("body"))
    switch = _mapping(restore.get("switch"))
    switch_body = _mapping(switch.get("body"))
    mode_after = _mapping(_mapping(restore.get("mode_after")).get("body"))
    mode_after_state = _mapping(mode_after.get("mode"))
    incident_after = _mapping(_mapping(restore.get("incident_after")).get("body"))

    entered_ok = bool(
        int(entered.get("status") or 0) == 200
        and str(entered_body.get("incident_id") or "")
        and _mapping(entered_body.get("mode")).get("current_mode") == "incident_lockdown"
        and incident_during.get("status") == "open"
        and mode_during.get("current_mode") == "incident_lockdown"
    )
    restrictions = bool(
        int(root_restricted.get("status") or 0) == 503
        and _mapping(root_restricted.get("body")).get("server_mode") == "incident_lockdown"
        and int(member_restricted.get("status") or 0) in {401, 403, 503}
        and int(root_recovery.get("status") or 0) == 200
    )
    diagnostics_ok = bool(
        int(repair.get("status") or 0) == 200
        and audit_after_repair.get("ok") is True
        and diagnostic_audit_state.get("enabled") is True
        and diagnostic_audit_state.get("ok") is True
        and diagnostic_audit_state.get("broken_at") is None
        and diagnostic_db_state.get("ok") is True
        and int(diagnostic_logs.get("broken_links") or 0) == 0
        and not list(diagnostic_logs.get("invalid_signatures") or [])
        and diagnostic_logs.get("result") == "PASS"
    )
    resolved_ok = bool(
        int(resolve.get("status") or 0) == 200
        and resolve_body.get("ok") is True
        and resolve_body.get("incident_id") == entered_body.get("incident_id")
        and incident_after.get("incident") is None
    )
    restored_mode = str(enter.get("mode_before") or "")
    mode_restored = bool(
        restored_mode
        and restored_mode != "incident_lockdown"
        and int(switch.get("status") or 0) == 200
        and switch_body.get("ok") is True
        and mode_after_state.get("current_mode") == restored_mode
    )

    readiness = _mapping(final_state.get("readiness"))
    audit = _mapping(_mapping(final_state.get("audit_integrity")).get("body"))
    audit_state = _mapping(audit.get("audit_integrity"))
    database = _mapping(_mapping(final_state.get("database_integrity")).get("body"))
    database_state = _mapping(database.get("database"))
    logs = _mapping(_mapping(final_state.get("mode_log_chain")).get("body"))
    points_job = _mapping(final_state.get("points_verify_job"))
    points_latest = _mapping(_mapping(final_state.get("points_verify_latest")).get("body"))
    points_verification = _mapping(points_latest.get("verification"))
    trading_job = _mapping(final_state.get("trading_verify_job"))
    trading_latest = _mapping(_mapping(final_state.get("trading_verify_latest")).get("body"))
    trading_verification = _mapping(trading_latest.get("verification"))
    final_ok = bool(
        readiness.get("ok") is True
        and audit_state.get("enabled") is True
        and audit_state.get("ok") is True
        and database_state.get("ok") is True
        and logs.get("result") == "PASS"
        and int(logs.get("broken_links") or 0) == 0
        and points_job.get("terminal_status") == "succeeded"
        and points_verification.get("ok") is True
        and not list(points_verification.get("errors") or [])
        and points_verification.get("financial_ok") is not False
        and trading_job.get("terminal_status") == "succeeded"
        and trading_verification.get("ok") is True
        and not list(trading_verification.get("errors") or [])
    )
    evidence = {
        "incident_enter": entered_ok,
        "incident_restrictions_effective": restrictions,
        "diagnostics_integrity_and_repair": diagnostics_ok,
        "incident_resolve": resolved_ok,
        "server_mode_restore": mode_restored,
        "readiness_security_log_finance_chain_verify": final_ok,
    }
    cleanup_complete = bool(
        mode_restored
        and incident_after.get("incident") is None
        and _mapping(final_state.get("site_config")).get("maintenance_mode") is False
    )
    return {
        "scenario_assertions": evidence,
        "terminal_assertions": {
            "all_domain_assertions_true": all(evidence.values()),
            "server_ready_after_restore": readiness.get("ok") is True,
            "incident_closed": resolved_ok,
        },
        "cleanup_assertions": {
            "incident_no_longer_open": cleanup_complete,
            "original_server_mode_and_maintenance_state_restored": cleanup_complete,
        },
        "details": {"source_count": 4, "restored_mode": restored_mode},
    }


def trading_workflow_assertions(
    background: Mapping[str, Any],
    cancel_race: Mapping[str, Any],
    custom: Mapping[str, Any],
    restart: Mapping[str, Any],
    final_state: Mapping[str, Any],
    cleanup: Mapping[str, Any],
) -> dict[str, Mapping[str, object]]:
    """Derive trading, lending, bot, and custom-workflow claims from state."""

    scenario = _mapping(background.get("scenario"))
    terminal = _mapping(scenario.get("domain_terminal"))
    background_terminal = _mapping(scenario.get("background_terminal"))
    stress = _mapping(scenario.get("concurrent_stress"))
    checks = {
        str(row.get("name") or ""): row.get("ok") is True
        for row in _rows(background.get("checks"))
    }

    cancel_results = _rows(cancel_race.get("cancel_results"))
    final_cancel_order = _mapping(cancel_race.get("final_order"))
    match_cancel = bool(
        terminal.get("limit_order_status") == "filled"
        and checks.get("background matched limit order without active member browser") is True
        and str(cancel_race.get("order_uuid") or "")
        and len(cancel_results) == 2
        and sum(1 for row in cancel_results if int(row.get("status") or 0) == 200 and _mapping(row.get("body")).get("status") == "cancelled") == 1
        and sum(1 for row in cancel_results if int(row.get("status") or 0) == 400) == 1
        and final_cancel_order.get("status") == "cancelled"
        and cancel_race.get("locked_points_increased") is True
        and cancel_race.get("locked_points_restored_exactly") is True
    )
    lending = bool(
        terminal.get("margin_liquidation_status") == "liquidated"
        and terminal.get("margin_take_profit_status") == "closed"
        and int(terminal.get("margin_interest_hours") or 0) >= 3
        and checks.get("margin liquidation seed open") is True
        and checks.get("margin take-profit seed open") is True
        and checks.get("margin interest seed open") is True
        and checks.get("background liquidated margin account without active member browser") is True
        and checks.get("background triggered margin take-profit without active member browser") is True
        and checks.get("background accrued margin interest without active member browser") is True
    )
    bots = bool(
        int(
            terminal.get("spot_stop_loss_quantity_units")
            if terminal.get("spot_stop_loss_quantity_units") is not None else -1
        ) == 0
        and int(
            terminal.get("spot_take_profit_quantity_units")
            if terminal.get("spot_take_profit_quantity_units") is not None else -1
        ) == 0
        and int(terminal.get("workflow_bot_triggered_runs") or 0) >= 1
        and int(terminal.get("dca_bot_triggered_runs") or 0) >= 1
        and int(terminal.get("grid_filled_orders") or 0) >= 1
        and checks.get("background triggered spot stop-loss without active member browser") is True
        and checks.get("background triggered spot take-profit without active member browser") is True
        and checks.get("background triggered workflow/conditional bot without active browser") is True
        and checks.get("background triggered DCA bot without active browser") is True
        and checks.get("background scanned grid bot and filled crossed grid order without active browser") is True
    )
    required_jobs = {
        "order_matching",
        "take_profit_stop_loss_scan",
        "bot_trigger_scan",
        "margin_liquidation_scan",
        "interest_accrual",
    }
    background_without_browser = bool(
        scenario.get("trigger_mode") == "auto"
        and scenario.get("member_contexts_closed_before_background") is True
        and scenario.get("root_context_closed_before_background") is True
        and required_jobs.issubset(set(background_terminal.get("recent_job_keys") or []))
        and required_jobs.issubset(set(_mapping(background_terminal.get("failure_counts"))))
        and all(
            int(value or 0) == 0
            for key, value in _mapping(background_terminal.get("failure_counts")).items()
            if key in required_jobs
        )
        and checks.get("background jobs have no recorded failures") is True
    )

    initial_save = _mapping(custom.get("initial_save"))
    edited_save = _mapping(custom.get("edited_save"))
    backtest = _mapping(custom.get("backtest"))
    bot_create = _mapping(custom.get("bot_create"))
    bot_enable = _mapping(custom.get("bot_enable"))
    scan_trigger = _mapping(custom.get("scan_trigger"))
    trade_order = _mapping(custom.get("trade_order"))
    custom_lifecycle = bool(
        custom.get("template_absent_before") is True
        and int(initial_save.get("status") or 0) == 200
        and _mapping(_mapping(initial_save.get("body")).get("template")).get("id") == custom.get("template_id")
        and int(edited_save.get("status") or 0) == 200
        and _mapping(_mapping(edited_save.get("body")).get("template")).get("label") == custom.get("edited_label")
        and custom.get("edited_template_visible") is True
        and int(backtest.get("status") or 0) == 200
        and _mapping(backtest.get("body")).get("ok") is True
        and int(_mapping(backtest.get("body")).get("candle_count") or 0) >= 2
        and int(_mapping(backtest.get("body")).get("trade_count") or 0) >= 1
        and int(bot_create.get("status") or 0) == 200
        and _mapping(_mapping(bot_create.get("body")).get("bot")).get("enabled") is False
        and int(bot_enable.get("status") or 0) == 200
        and _mapping(_mapping(bot_enable.get("body")).get("bot")).get("enabled") is True
        and scan_trigger.get("bot_uuid") == custom.get("bot_uuid")
        and str(scan_trigger.get("order_uuid") or "")
        and trade_order.get("order_uuid") == scan_trigger.get("order_uuid")
        and trade_order.get("status") == "filled"
    )
    restart_persistence = bool(
        int(restart.get("old_pid") or 0) > 0
        and int(restart.get("new_pid") or 0) > 0
        and int(restart.get("new_pid") or 0) != int(restart.get("old_pid") or 0)
        and restart.get("old_master_remaining") is False
        and restart.get("old_process_group_remaining") is False
        and _mapping(restart.get("readiness")).get("ok") is True
        and restart.get("template_found") is True
        and restart.get("bot_found") is True
        and restart.get("trade_order_found") is True
        and restart.get("template_file_hash_preserved") is True
    )
    full_stress = bool(
        int(stress.get("requested_per_user") or 0) >= 150
        and int(stress.get("request_count") or 0) >= 300
        and int(stress.get("success_count") or 0) > 0
        and stress.get("no_5xx") is True
        and checks.get("Playwright concurrent order stress has no 5xx and produces fills") is True
    )

    final_trading_job = _mapping(final_state.get("trading_verify_job"))
    final_trading_latest = _mapping(_mapping(final_state.get("trading_verify_latest")).get("body"))
    final_trading_verify = _mapping(final_trading_latest.get("verification"))
    final_points_job = _mapping(final_state.get("points_verify_job"))
    final_points_latest = _mapping(_mapping(final_state.get("points_verify_latest")).get("body"))
    final_points_verify = _mapping(final_points_latest.get("verification"))
    reserve_and_nonnegative = bool(
        not list(terminal.get("negative_wallet_rows") or [])
        and not list(terminal.get("negative_spot_lock_rows") or [])
        and int(terminal.get("reserve_before") or 0) >= 0
        and int(
            terminal.get("reserve_after")
            if terminal.get("reserve_after") is not None else -1
        ) >= int(terminal.get("reserve_before") or 0)
        and final_trading_job.get("terminal_status") == "succeeded"
        and final_trading_verify.get("ok") is True
        and not list(final_trading_verify.get("errors") or [])
        and final_points_job.get("terminal_status") == "succeeded"
        and final_points_verify.get("ok") is True
        and not list(final_points_verify.get("errors") or [])
        and final_points_verify.get("financial_ok") is not False
        and int(
            final_state.get("reserve_balance_points")
            if final_state.get("reserve_balance_points") is not None else -1
        ) >= 0
    )
    evidence = {
        "spot_order_match_cancel_race": match_cancel,
        "lending_margin_collateral_interest_liquidation": lending,
        "grid_dca_conditional_tp_sl": bots,
        "background_worker_without_browser": background_without_browser,
        "custom_workflow_create_edit_backtest_enable_trade": custom_lifecycle,
        "custom_workflow_restart_persistence": restart_persistence,
        "full_concurrent_stress_mode": full_stress,
        "reserve_ledger_and_nonnegative_invariants": reserve_and_nonnegative,
    }

    fixture_usernames = sorted({
        str(_mapping(row).get("username") or "")
        for row in _mapping(scenario.get("users")).values()
        if str(_mapping(row).get("username") or "")
    })
    cleanup_rows = _rows(cleanup.get("account_records"))
    accounts_removed = bool(
        fixture_usernames
        and {str(row.get("username") or "") for row in cleanup_rows} == set(fixture_usernames)
        and all(
            row.get("deleted") is True
            and int(row.get("residual_exact_count") or 0) == 0
            for row in cleanup_rows
        )
    )
    cleanup_complete = bool(
        accounts_removed
        and cleanup.get("bot_deleted") is True
        and cleanup.get("bot_absent") is True
        and cleanup.get("template_file_removed") is True
        and cleanup.get("template_absent_from_api") is True
        and cleanup.get("custom_user_directory_absent") is True
        and scenario.get("runtime_settings_restored") is True
        and scenario.get("feature_flags_restored") is True
    )
    return {
        "scenario_assertions": evidence,
        "terminal_assertions": {
            "all_domain_assertions_true": all(evidence.values()),
            "primary_ready_after_restart": _mapping(final_state.get("readiness")).get("ok") is True,
            "trading_and_pointschain_verify_succeeded": reserve_and_nonnegative,
        },
        "cleanup_assertions": {
            "exact_fixture_accounts_removed": accounts_removed,
            "custom_bot_and_workflow_removed": cleanup_complete,
            "mutated_runtime_settings_restored": cleanup_complete,
        },
        "details": {
            "source_count": 6,
            "fixture_account_count": len(fixture_usernames),
            "stress_request_count": int(stress.get("request_count") or 0),
        },
    }


def cloud_drive_stream_assertions(
    probe: Mapping[str, Any],
) -> dict[str, Mapping[str, object]]:
    """Derive cloud upload, HLS/share/proxy/UI claims from the live probe."""

    fixture = _mapping(probe.get("fixture"))
    upload = _mapping(probe.get("upload"))
    worker = _mapping(probe.get("hls_worker"))
    worker_payload = _mapping(worker.get("payload"))
    stream = _mapping(probe.get("stream"))
    share = _mapping(probe.get("share"))
    proxy = _mapping(share.get("realtime_proxy"))
    browser = _mapping(probe.get("browser"))
    browser_rows = _rows(browser.get("rows"))
    cleanup = _mapping(probe.get("cleanup"))

    cloud_upload = bool(
        float(fixture.get("duration_seconds") or 0) >= 10
        and int(fixture.get("video_streams") or 0) >= 1
        and int(fixture.get("audio_streams") or 0) >= 2
        and int(fixture.get("subtitle_streams") or 0) >= 1
        and len(str(fixture.get("sha256") or "")) == 64
        and int(upload.get("status") or 0) == 200
        and str(_mapping(_mapping(upload.get("body")).get("storage_file")).get("id") or "")
        and str(_mapping(_mapping(upload.get("body")).get("storage_file")).get("file_id") or "")
    )
    terminal_ready = bool(
        int(worker.get("returncode") if worker.get("returncode") is not None else -1) == 0
        and worker_payload.get("ok") is True
        and stream.get("status") == "ready"
        and stream.get("master_manifest_ready") is True
    )
    password_unlock = bool(
        int(share.get("password_required_status") or 0) == 401
        and int(share.get("wrong_password_status") or 0) == 403
        and int(_mapping(share.get("unlocked")).get("status") or 0) == 200
        and str(share.get("share_id") or "")
        and str(share.get("token") or "")
    )
    hls_share = bool(
        int(share.get("audio_track_count") or 0) >= 2
        and int(share.get("subtitle_count") or 0) >= 1
        and int(share.get("master_status") or 0) == 200
        and share.get("master_extm3u") is True
        and int(share.get("variant_status") or 0) == 200
        and share.get("variant_extm3u") is True
        and int(share.get("segment_status") or 0) == 200
        and int(share.get("segment_bytes") or 0) > 0
        and int(share.get("subtitle_status") or 0) == 200
        and share.get("subtitle_webvtt") is True
    )
    realtime = bool(
        int(proxy.get("status") or 0) == 200
        and proxy.get("streaming_mode") == "realtime_proxy"
        and str(proxy.get("transfer_mode") or "").startswith("python_realtime_proxy")
        and int(proxy.get("first_chunk_bytes") or 0) > 0
    )
    desktop_mobile = bool(
        browser.get("browser_closed") is True
        and {str(row.get("viewport") or "") for row in browser_rows}
        == {"desktop", "mobile"}
        and all(
            _mapping(row.get("state")).get("player_present") is True
            and int(_mapping(row.get("state")).get("root_overflow_px") or 0) == 0
            and not list(row.get("page_errors") or [])
            and int(row.get("screenshot_size_bytes") or 0) > 0
            and row.get("context_closed") is True
            for row in browser_rows
        )
    )
    revoked = bool(
        int(_mapping(cleanup.get("revoke")).get("status") or 0) == 200
        and int(cleanup.get("revoked_access_status") or 0) in {404, 410}
    )
    evidence = {
        "cloud_video_upload": cloud_upload,
        "cloud_stream_prepare_terminal_ready": terminal_ready,
        "storage_share_password_unlock": password_unlock,
        "storage_share_master_variant_segment_subtitle": hls_share,
        "storage_share_realtime_proxy": realtime,
        "storage_share_desktop_mobile_playback": desktop_mobile,
        "storage_share_revoke_denial": revoked,
    }
    fixture_path = Path(str(fixture.get("path") or ""))
    screenshots = [Path(str(row.get("screenshot") or "")) for row in browser_rows]
    artifact_files_exist = bool(
        fixture_path.is_file()
        and all(path.is_file() and path.stat().st_size > 0 for path in screenshots)
    )
    product_cleanup = bool(
        int(_mapping(cleanup.get("trash")).get("status") or 0) == 200
        and int(_mapping(cleanup.get("purge")).get("status") or 0) == 200
        and int(cleanup.get("owner_preview_after_purge_status") or 0) == 404
    )
    return {
        "scenario_assertions": evidence,
        "terminal_assertions": {
            "all_domain_assertions_true": all(evidence.values()),
            "probe_terminal_pass": probe.get("ok") is True and not list(probe.get("errors") or []),
            "hls_worker_and_share_terminal": terminal_ready and hls_share,
        },
        "cleanup_assertions": {
            "share_revoked_and_product_file_purged": revoked and product_cleanup,
            "browser_contexts_closed": desktop_mobile,
            "declared_fixture_and_screenshots_readable": artifact_files_exist,
        },
        "details": {
            "source_count": 1 + len(screenshots),
            "browser_viewport_count": len(browser_rows),
            "fixture_size_bytes": int(fixture.get("size_bytes") or 0),
        },
    }


def community_governance_assertions(
    probe: Mapping[str, Any],
) -> dict[str, Mapping[str, object]]:
    """Derive forum/chat/friend/governance claims from the strict live probe."""

    forum = _mapping(probe.get("forum"))
    thread = _mapping(forum.get("thread"))
    post = _mapping(forum.get("post"))
    report = _mapping(forum.get("terminal_report"))
    chat = _mapping(probe.get("chat"))
    private_room = _mapping(_mapping(chat.get("private_room")).get("body"))
    private_room = _mapping(private_room.get("room"))
    terminal_message = _mapping(chat.get("terminal_message"))
    notification = _mapping(chat.get("notification"))
    friends = _mapping(probe.get("friends"))
    accepted = _mapping(friends.get("accept_terminal"))
    profile = _mapping(_mapping(friends.get("profile")).get("profile"))
    block = _mapping(_mapping(_mapping(friends.get("block")).get("body")).get("block"))
    blocked_state = _mapping(friends.get("blocked_state"))
    blocked_dm = _mapping(friends.get("blocked_dm"))
    unblock = _mapping(friends.get("unblock"))
    actors = _mapping(probe.get("actors"))
    user_one = _mapping(actors.get("user_one"))
    user_two = _mapping(actors.get("user_two"))
    user_one_id = int(user_one.get("id") or 0)
    user_two_id = int(user_two.get("id") or 0)
    governance = _mapping(probe.get("governance"))
    terminal_proposal = _mapping(governance.get("terminal_proposal"))
    vote = _mapping(_mapping(governance.get("vote")).get("body"))
    voted_proposal = _mapping(vote.get("proposal"))
    boundaries = _mapping(probe.get("boundaries"))
    rate = _mapping(boundaries.get("chat_rate_limit"))
    rate_terminal = _mapping(rate.get("terminal"))
    browser = _mapping(probe.get("browser"))
    browser_rows = _rows(browser.get("rows"))
    cleanup = _mapping(probe.get("cleanup"))

    forum_lifecycle = bool(
        int(forum.get("thread_id") or 0) > 0
        and int(thread.get("id") or 0) == int(forum.get("thread_id") or 0)
        and thread.get("status") == "approved"
        and int(post.get("id") or 0) > 0
        and int(forum.get("report_id") or 0) > 0
        and int(report.get("id") or 0) == int(forum.get("report_id") or 0)
        and report.get("status") == "rejected"
        and str(report.get("claimed_by_username") or "")
        and str(report.get("reviewed_by") or "")
    )
    chat_notifications = bool(
        int(chat.get("private_room_id") or 0) > 0
        and int(private_room.get("id") or 0) == int(chat.get("private_room_id") or 0)
        and bool(private_room.get("is_private"))
        and int(terminal_message.get("id") or 0) > 0
        and str(terminal_message.get("content") or "")
        and int(terminal_message.get("sender_id") or 0) > 0
        and int(notification.get("id") or 0) > 0
        and notification.get("type") == "chat_private_message"
    )
    accepted_rows = _rows(accepted.get("friends"))
    blocked_rows = _rows(blocked_state.get("blocked"))
    friend_profile_block = bool(
        user_one_id > 0
        and user_two_id > 0
        and any(int(row.get("other_user_id") or 0) == user_two_id for row in accepted_rows)
        and int(profile.get("id") or profile.get("user_id") or 0) == user_two_id
        and block.get("status") == "blocked"
        and int(_mapping(block.get("target")).get("id") or 0) == user_two_id
        and any(int(row.get("other_user_id") or 0) == user_two_id for row in blocked_rows)
        and int(blocked_dm.get("status") or 0) == 403
        and int(unblock.get("status") or 0) == 200
        and _mapping(unblock.get("body")).get("ok") is True
    )
    governance_lifecycle = bool(
        int(governance.get("proposal_id") or 0) > 0
        and int(terminal_proposal.get("id") or 0) == int(governance.get("proposal_id") or 0)
        and terminal_proposal.get("action_type") == "warn"
        and terminal_proposal.get("status") == "executed"
        and int(voted_proposal.get("id") or 0) == int(governance.get("proposal_id") or 0)
        and voted_proposal.get("status") == "approved"
        and int(_mapping(governance.get("proposer_vote_denied")).get("status") or 0) == 403
    )
    role_rate_boundaries = bool(
        int(_mapping(boundaries.get("member_governance_denied")).get("status") or 0) == 403
        and int(_mapping(boundaries.get("csrf_missing_denied")).get("status") or 0) in {400, 403}
        and int(rate.get("attempt_count") or 0) >= 2
        and int(rate.get("success_count") or 0) >= 1
        and int(rate_terminal.get("status") or 0) == 429
        and _mapping(rate_terminal.get("body")).get("ok") is False
    )
    desktop_mobile = bool(
        browser.get("browser_closed") is True
        and {str(row.get("viewport") or "") for row in browser_rows} == {"desktop", "mobile"}
        and all(
            _mapping(row.get("community")).get("active") is True
            and str(thread.get("title") or "") in str(_mapping(row.get("community")).get("thread_text") or "")
            and int(_mapping(row.get("community")).get("overflow_px") or 0) <= 1
            and _mapping(row.get("chat")).get("active") is True
            and str(terminal_message.get("content") or "") in str(_mapping(row.get("chat")).get("messages") or "")
            and int(_mapping(row.get("chat")).get("overflow_px") or 0) <= 1
            and not list(row.get("console_errors") or [])
            and not list(row.get("page_errors") or [])
            and not list(row.get("failed_responses") or [])
            and int(row.get("screenshot_size_bytes") or 0) > 0
            and Path(str(row.get("screenshot") or "")).is_file()
            and row.get("context_closed") is True
            for row in browser_rows
        )
    )
    evidence = {
        "forum_thread_reply_report_moderate": forum_lifecycle,
        "chat_private_message_and_notifications": chat_notifications,
        "friends_profiles_and_blocking": friend_profile_block,
        "social_proposal_vote_execute": governance_lifecycle,
        "role_permission_and_rate_limit_boundaries": role_rate_boundaries,
        "desktop_mobile_community_ui": desktop_mobile,
    }
    fixture_cleanup = bool(
        cleanup.get("thread_deleted") is True
        and cleanup.get("thread_denied_to_member") is True
        and cleanup.get("private_room_deleted") is True
        and cleanup.get("private_room_absent") is True
        and cleanup.get("rate_room_deleted") is True
        and cleanup.get("rate_room_absent") is True
        and cleanup.get("friendship_absent") is True
        and cleanup.get("block_absent") is True
        and cleanup.get("settings_restored") is True
        and cleanup.get("notifications_dismissed") is True
        and isinstance(cleanup.get("notification_ids_dismissed"), list)
        and bool(cleanup.get("notification_ids_dismissed"))
        and not list(cleanup.get("cleanup_errors") or [])
    )
    return {
        "scenario_assertions": evidence,
        "terminal_assertions": {
            "all_domain_assertions_true": all(evidence.values()),
            "probe_terminal_pass": probe.get("ok") is True and not list(probe.get("errors") or []),
            "report_and_proposal_terminal": report.get("status") == "rejected"
            and terminal_proposal.get("status") == "executed",
        },
        "cleanup_assertions": {
            "reversible_community_chat_friend_fixtures_removed": fixture_cleanup,
            "feature_settings_restored": cleanup.get("settings_restored") is True,
            "browser_contexts_closed_and_screenshots_readable": desktop_mobile,
        },
        "details": {
            "source_count": 1 + len(browser_rows),
            "browser_viewport_count": len(browser_rows),
            "dismissed_notification_count": len(cleanup.get("notification_ids_dismissed") or []),
        },
    }


def ai_agent_positive_assertions(
    probe: Mapping[str, Any],
    restart: Mapping[str, Any],
) -> dict[str, Mapping[str, object]]:
    """Derive strict AI write-operation and supervised-restart evidence."""

    catalogs = _mapping(probe.get("catalogs"))
    root_catalog = _mapping(catalogs.get("root"))
    manager_catalog = _mapping(catalogs.get("manager"))
    user_catalog = _mapping(catalogs.get("user"))
    root_names = {str(item) for item in (root_catalog.get("names") or [])}
    manager_names = {str(item) for item in (manager_catalog.get("names") or [])}
    user_names = {str(item) for item in (user_catalog.get("names") or [])}
    role_catalog = bool(
        root_catalog.get("actor_role") == "super_admin"
        and manager_catalog.get("actor_role") == "manager"
        and user_catalog.get("actor_role") == "user"
        and all(row.get("role_scoped") is True for row in (root_catalog, manager_catalog, user_catalog))
        and all(row.get("write_enabled") is True for row in (root_catalog, manager_catalog, user_catalog))
        and all(len(str(row.get("catalog_sha256") or "")) == 64 for row in (root_catalog, manager_catalog, user_catalog))
        and {
            "write_server_restart",
            "write_incident_enter",
            "write_incident_resolve",
            "write_appeal_review",
            "write_trading_verify_jobs",
        }.issubset(root_names)
        and "write_governance_vote" in manager_names
        and "write_server_restart" not in manager_names
        and "write_appeal_review" not in manager_names
        and {
            "write_cloud_drive_create_text",
            "write_video_publish",
            "write_trading_place_order",
            "write_appeal_create",
        }.issubset(user_names)
        and "write_governance_vote" not in user_names
        and "write_server_restart" not in user_names
    )

    settings = _mapping(probe.get("settings"))
    settings_before = _mapping(settings.get("before"))
    settings_enabled = _mapping(settings.get("enabled"))
    cleanup = _mapping(probe.get("cleanup"))
    settings_lifecycle = bool(
        len(settings_before) == 12
        and settings.get("enabled_readback") is True
        and settings_enabled.get("feature_ai_agent_enabled") is True
        and settings_enabled.get("audit_chain_enabled") is True
        and settings_enabled.get("audit_chain_reseal_required") is False
        and settings_enabled.get("ai_agent_operation_mode") == "write"
        and settings_enabled.get("module_ai_agent_min_role") == "user"
        and cleanup.get("settings_restored") is True
    )

    orchestration = _mapping(probe.get("orchestration"))
    orchestration_write_plan = _mapping(orchestration.get("write_plan"))
    orchestration_write_request = _mapping(orchestration.get("write_request"))
    orchestration_write_terminal = _mapping(orchestration.get("write_terminal"))
    orchestration_readonly_plan = _mapping(orchestration.get("readonly_plan"))
    orchestration_readonly_terminal = _mapping(orchestration.get("readonly_terminal"))
    orchestration_cleanup = _mapping(orchestration.get("cleanup"))
    orchestration_browser = _mapping(orchestration.get("browser"))
    real_orchestration = bool(
        orchestration.get("real_provider") is True
        and len(orchestration.get("provider_models") or []) >= 1
        and int(orchestration.get("chat_call_count") or 0) >= 2
        and orchestration_write_plan.get("action") == "write_tool"
        and orchestration_write_plan.get("tool") == "write_album_create"
        and orchestration_write_plan.get("execute_write") is True
        and str(orchestration_write_plan.get("planner_strategy") or "")
        not in {"local_fast_path", "deterministic_fallback"}
        and not str(orchestration_write_plan.get("fallback_error") or "")
        and orchestration.get("write_handled") is True
        and orchestration_write_request.get("tool") == "write_album_create"
        and orchestration_write_request.get("confirm") == "EXECUTE"
        and int(orchestration_write_terminal.get("status") or 0) == 200
        and orchestration_write_terminal.get("ok") is True
        and str(orchestration_write_terminal.get("album_id") or "")
        and str(orchestration_write_terminal.get("title") or "")
        and orchestration_write_terminal.get("visibility") == "private"
        and orchestration_readonly_plan.get("action") == "readonly"
        and str(orchestration_readonly_plan.get("readonly_scope") or "")
        in {"server_mode", "resources", "all"}
        and str(orchestration_readonly_plan.get("planner_strategy") or "")
        not in {"local_fast_path", "deterministic_fallback"}
        and not str(orchestration_readonly_plan.get("fallback_error") or "")
        and orchestration.get("readonly_handled") is True
        and int(orchestration_readonly_terminal.get("status") or 0) == 200
        and orchestration_readonly_terminal.get("ok") is True
        and orchestration_cleanup.get("album_absent") is True
        and int(orchestration_cleanup.get("album_absent_status") or 0) == 404
        and not list(orchestration_browser.get("page_errors") or [])
        and not list(orchestration_browser.get("console_errors") or [])
    )
    # Keep the reviewed ten-ID formal binding stable: the catalog evidence is
    # only true when the catalog is not merely enumerable, but also usable by
    # the shipped UI with a real provider for one confirmed write and one
    # readonly operations-assistance request.
    role_catalog = bool(role_catalog and real_orchestration)

    drive = _mapping(probe.get("drive"))
    owner_content = _mapping(drive.get("owner_content"))
    shared_content = _mapping(drive.get("shared_content"))
    drive_lifecycle = bool(
        _mapping(drive.get("create")).get("ok") is True
        and int(owner_content.get("status") or 0) == 200
        and int(owner_content.get("size_bytes") or 0) > 0
        and owner_content.get("exact") is True
        and owner_content.get("sha256") == owner_content.get("expected_sha256")
        and _mapping(drive.get("share_create")).get("ok") is True
        and _mapping(drive.get("share_update")).get("ok") is True
        and int(drive.get("terminal_max_views") or 0) == 7
        and int(drive.get("public_access_status") or 0) == 200
        and int(shared_content.get("status") or 0) == 200
        and int(shared_content.get("size_bytes") or 0) > 0
        and shared_content.get("exact") is True
        and shared_content.get("sha256") == shared_content.get("expected_sha256")
        and _mapping(drive.get("share_revoke")).get("ok") is True
        and int(drive.get("revoked_access_status") or 0) in {404, 410}
        and _mapping(drive.get("delete")).get("ok") is True
        and drive.get("file_absent") is True
        and len(str(drive.get("share_token_sha256") or "")) == 64
    )

    video = _mapping(probe.get("video"))
    fixture = _mapping(video.get("fixture"))
    video_terminal = _mapping(video.get("terminal"))
    hls = _mapping(video.get("hls"))
    video_lifecycle = bool(
        int(fixture.get("size_bytes") or 0) > 0
        and float(fixture.get("duration_seconds") or 0) >= 5
        and len(str(fixture.get("sha256") or "")) == 64
        and _mapping(video.get("publish")).get("ok") is True
        and int(video.get("published_video_id") or 0) > 0
        and video_terminal.get("status") == "ready"
        and video_terminal.get("streaming_ready") is True
        and video_terminal.get("mode") == "hls"
        and video_terminal.get("master_url_present") is True
        and int(hls.get("master_status") or 0) == 200
        and int(hls.get("variant_status") or 0) == 200
        and int(hls.get("segment_status") or 0) == 200
        and int(hls.get("segment_bytes") or 0) > 0
        and _mapping(video.get("delete_video")).get("ok") is True
        and _mapping(video.get("delete_cloud_file")).get("ok") is True
        and int(video.get("playback_after_delete_status") or 0) == 404
    )

    trading = _mapping(probe.get("trading"))
    spot = _mapping(trading.get("spot"))
    margin = _mapping(trading.get("margin_lending"))
    bot = _mapping(trading.get("custom_workflow_bot"))
    funding_before = _mapping(trading.get("funding_before"))
    funding_after_spot = _mapping(spot.get("funding_after"))
    funding_after_margin = _mapping(margin.get("funding_after"))
    pool_before = _mapping(margin.get("funding_pool_before"))
    pool_after = _mapping(margin.get("funding_pool_after"))
    funding_after_bot = _mapping(bot.get("funding_after"))
    invariants = _mapping(trading.get("invariants"))
    invariant_job = _mapping(invariants.get("job"))
    triggered = _mapping(bot.get("scan_triggered"))
    trading_lifecycle = bool(
        _mapping(spot.get("create")).get("ok") is True
        and str(spot.get("order_uuid") or "")
        and _mapping(spot.get("cancel")).get("ok") is True
        and spot.get("terminal_status") == "cancelled"
        and bool(funding_before)
        and funding_after_spot == funding_before
        and _mapping(margin.get("open")).get("ok") is True
        and margin.get("initial_status") == "open"
        and str(margin.get("borrowed_asset_symbol") or "")
        and int(margin.get("principal_points") or 0) > 0
        and _mapping(margin.get("close")).get("ok") is True
        and margin.get("terminal_status") == "closed"
        and funding_after_margin.get("locked_points") == funding_before.get("locked_points")
        and funding_after_margin.get("wallet_locked_points") == funding_before.get("wallet_locked_points")
        and funding_after_margin.get("trial_locked_points") == funding_before.get("trial_locked_points")
        and pool_after.get("outstanding_principal_points") == pool_before.get("outstanding_principal_points")
        and pool_after.get("capacity_points") == pool_before.get("capacity_points")
        and int(pool_after.get("balance_points") or 0) >= int(pool_before.get("balance_points") or 0)
        and _mapping(bot.get("create")).get("ok") is True
        and str(bot.get("workflow_source") or "").startswith("formal_ai_agent_")
        and int(bot.get("workflow_node_count") or 0) >= 3
        and _mapping(bot.get("scan")).get("ok") is True
        and str(triggered.get("bot_uuid") or "") == str(bot.get("bot_uuid") or "")
        and str(triggered.get("order_uuid") or "")
        and int(bot.get("run_count") or 0) == 1
        and _mapping(bot.get("cancel_order")).get("ok") is True
        and bot.get("cancelled_order_terminal_status") == "cancelled"
        and funding_after_bot == funding_after_margin
        and int(bot.get("delete_status") or 0) == 200
        and bot.get("absent") is True
        and _mapping(invariants.get("verify")).get("ok") is True
        and invariant_job.get("terminal_status") == "succeeded"
        and invariants.get("verification_ok") is True
        and not list(invariants.get("errors") or [])
    )

    community = _mapping(probe.get("community"))
    governance = _mapping(probe.get("governance"))
    persistent_rewards = _mapping(community.get("persistent_rewards"))
    thread_reward = _mapping(persistent_rewards.get("thread_author"))
    reply_reward = _mapping(persistent_rewards.get("reply_author"))
    community_governance = bool(
        _mapping(community.get("create")).get("ok") is True
        and _mapping(community.get("reply")).get("ok") is True
        and int(community.get("thread_id") or 0) > 0
        and str(community.get("terminal_title") or "")
        and int(community.get("terminal_reply_id") or 0) > 0
        and persistent_rewards.get("accounted") is True
        and thread_reward.get("action_type") == "forum_post_reward"
        and thread_reward.get("reference_type") == "forum_thread"
        and str(thread_reward.get("reference_id") or "") == str(community.get("thread_id") or "")
        and int(thread_reward.get("amount") or 0) > 0
        and int(thread_reward.get("balance_after") or 0)
        == int(thread_reward.get("balance_before") or 0) + int(thread_reward.get("amount") or 0)
        and reply_reward.get("action_type") == "forum_comment_reward"
        and reply_reward.get("reference_type") == "forum_post"
        and str(reply_reward.get("reference_id") or "") == str(community.get("terminal_reply_id") or "")
        and int(reply_reward.get("amount") or 0) > 0
        and int(reply_reward.get("balance_after") or 0)
        == int(reply_reward.get("balance_before") or 0) + int(reply_reward.get("amount") or 0)
        and int(community.get("delete_status") or 0) == 200
        and int(community.get("absent_status") or 0) == 404
        and _mapping(governance.get("create")).get("ok") is True
        and _mapping(governance.get("vote")).get("ok") is True
        and governance.get("approved_status") == "approved"
        and _mapping(governance.get("execute")).get("ok") is True
        and governance.get("terminal_status") == "executed"
        and governance.get("action_type") == "warn"
        and int(governance.get("violation_id") or 0) > 0
        and int(governance.get("violation_count_after_warning") or 0)
        == int(governance.get("violation_count_before") or 0) + 1
        and _mapping(governance.get("appeal_create")).get("ok") is True
        and int(governance.get("appeal_id") or 0) > 0
        and _mapping(governance.get("appeal_review")).get("ok") is True
        and governance.get("appeal_terminal_status") == "approved"
        and governance.get("violation_count_restored") == governance.get("violation_count_before")
        and governance.get("account_state_restored") is True
    )

    launch = _mapping(probe.get("launch"))
    launch_logs = _mapping(launch.get("logs_verify"))
    launch_dry_run = bool(
        _mapping(launch.get("preflight")).get("ok") is True
        and launch.get("dry_run") is True
        and launch.get("auto_switch") is False
        and launch.get("mode_before") == launch.get("mode_after")
        and isinstance(launch.get("preflight_passed"), bool)
        and int(launch.get("blocker_count") or 0) == len(launch.get("blockers") or [])
        and launch.get("preflight_passed") == (not list(launch.get("blockers") or []))
        and launch.get("outcome_consistent") is True
        and set(launch.get("step_names") or [])
        == {"requirements_gate", "log_chain_verify", "ai_agent_audit_scan", "switch_production", "final_mode_status"}
        and launch_logs.get("ok") is True
        and int(launch_logs.get("broken_links") or 0) == 0
    )

    incident = _mapping(probe.get("incident"))
    active_terminal = _mapping(incident.get("active_terminal"))
    resolved_terminal = _mapping(incident.get("resolved_terminal"))
    incident_lifecycle = bool(
        _mapping(incident.get("enter")).get("ok") is True
        and _mapping(incident.get("enter_root_relogin")).get("ok") is True
        and (active_terminal.get("active") is True or active_terminal.get("status") in {"active", "open"})
        and _mapping(incident.get("resolve")).get("ok") is True
        and _mapping(incident.get("resolve_root_relogin")).get("ok") is True
        and resolved_terminal.get("active") is not True
        and resolved_terminal.get("status") not in {"active", "open"}
        and str(incident.get("mode_before") or "")
        and incident.get("mode_after") == incident.get("mode_before")
    )

    restart_request = _mapping(probe.get("restart_request"))
    restart_lifecycle = bool(
        _mapping(restart_request.get("tool")).get("ok") is True
        and restart_request.get("mode") == "supervised-request"
        and restart_request.get("requires_supervisor_restart") is True
        and restart_request.get("request_schema_version") == "hackme.supervised-restart-request/v1"
        and restart_request.get("receipt_schema_version") == "hackme.supervised-restart-request/v1"
        and restart_request.get("receipt_nonce_matches") is True
        and restart_request.get("reason_matches_request") is True
        and restart.get("schema_version") == "hackme.formal-ai-agent-supervised-restart/v1"
        and restart.get("receipt_valid") is True
        and restart.get("receipt_nonce_matches_probe") is True
        and restart.get("requesting_pid_in_old_tree") is True
        and int(restart.get("before_pid") or 0) > 0
        and int(restart.get("after_pid") or 0) > 0
        and restart.get("before_pid") != restart.get("after_pid")
        and restart.get("old_tree_gone") is True
        and restart.get("outage_observed") is True
        and restart.get("post_restart_ready") is True
        and restart.get("restart_request_removed") is True
        and _mapping(restart.get("restart")).get("ok") is True
    )

    audit = _mapping(probe.get("audit"))
    expected_tool_calls = _mapping(audit.get("expected_tool_calls"))
    audited_tool_calls = _mapping(audit.get("audited_tool_calls"))
    secure_audit_chain = _mapping(audit.get("secure_audit_chain"))
    audit_lifecycle = bool(
        audit.get("required_tools_present") is True
        and not _mapping(audit.get("missing_expected_tool_calls"))
        and bool(expected_tool_calls)
        and set(expected_tool_calls) == set(audited_tool_calls)
        and all(
            int(audited_tool_calls.get(key) or 0) >= int(expected_tool_calls.get(key) or 0)
            for key in expected_tool_calls
        )
        and int(audit.get("expected_tool_call_count") or 0) > 0
        and int(audit.get("audited_expected_tool_call_count") or 0)
        == int(audit.get("expected_tool_call_count") or 0)
        and int(audit.get("audit_last_id") or 0) > int(audit.get("audit_start_id") or 0)
        and secure_audit_chain.get("enabled") is True
        and secure_audit_chain.get("ok") is True
        and secure_audit_chain.get("broken_at") in {None, 0, ""}
        and audit.get("log_chain_verified") is True
        and audit.get("audit_scan_terminal") is True
    )

    evidence = {
        "role_scoped_tool_catalog": role_catalog,
        "settings_snapshot_and_restore": settings_lifecycle,
        "drive_share_create_update_revoke_delete": drive_lifecycle,
        "video_hls_publish_and_terminal_job": video_lifecycle,
        "spot_margin_lending_bot_workflow_operations": trading_lifecycle,
        "community_and_governance_operations": community_governance,
        "launch_preflight_dry_run": launch_dry_run,
        "incident_enter_resolve_and_mode_restore": incident_lifecycle,
        "scheduled_restart_outage_and_readiness": restart_lifecycle,
        "write_audit_chain_verify": audit_lifecycle,
    }
    cleanup_ok = bool(
        cleanup.get("settings_restored") is True
        and cleanup.get("orchestration_album_absent") is True
        and cleanup.get("drive_fixture_absent") is True
        and cleanup.get("video_fixture_absent") is True
        and cleanup.get("trading_orders_terminal") is True
        and cleanup.get("custom_workflow_bot_absent") is True
        and cleanup.get("community_thread_absent") is True
        and cleanup.get("community_persistent_rewards_accounted") is True
        and cleanup.get("governance_account_restored") is True
        and cleanup.get("incident_resolved") is True
        and not list(cleanup.get("errors") or [])
    )
    return {
        "scenario_assertions": evidence,
        "terminal_assertions": {
            "all_domain_assertions_true": all(evidence.values()),
            "probe_terminal_pass": probe.get("ok") is True and not list(probe.get("errors") or []),
            "supervised_restart_terminal_pass": restart_lifecycle,
        },
        "cleanup_assertions": {
            "reversible_fixtures_removed_and_persistent_rewards_accounted": cleanup_ok,
            "restart_receipt_consumed_exactly_once": restart.get("restart_request_removed") is True,
            "old_managed_process_tree_gone": restart.get("old_tree_gone") is True,
        },
        "details": {
            "source_count": 3,
            "ai_tool_success_count": int(audit.get("ai_tool_success_count") or 0),
            "catalog_tool_counts": {
                "root": int(root_catalog.get("tool_count") or 0),
                "manager": int(manager_catalog.get("tool_count") or 0),
                "user": int(user_catalog.get("tool_count") or 0),
            },
            "restart_outage_sample_count": int(restart.get("outage_sample_count") or 0),
            "real_provider_chat_call_count": int(orchestration.get("chat_call_count") or 0),
            "real_provider_models": list(orchestration.get("provider_models") or []),
        },
    }


def comfyui_workflow_assertions(
    probe: Mapping[str, Any],
    artifact_index: Mapping[str, Any],
) -> dict[str, Mapping[str, object]]:
    """Derive formal real-ComfyUI claims from terminal output and cleanup state.

    The probe's top-level ``ok`` and contract booleans are deliberately not
    sufficient here.  Every evidence ID is recomputed from the underlying
    backend, child-probe, job, UI, failure-injection, cleanup, and artifact
    observations.  The artifact index is also reopened and every recorded
    digest is checked before it can support a formal result.
    """

    evidence_ids = {
        "real_backend_required",
        "feature_probe",
        "official_templates_execute",
        "custom_workflow_create_import_run_output_delete",
        "ai_agent_generation_terminal_output",
        "desktop_mobile_workflow_ui",
        "offline_and_dependency_failure_visible",
    }
    expected_workflows = set(SYSTEM_WORKFLOW_IDS)
    sections = _mapping(probe.get("sections"))
    real_backend = _mapping(sections.get("real_backend"))
    safety = _mapping(sections.get("safety"))
    safe_selection = _mapping(safety.get("selection"))
    safe_canary = _mapping(safety.get("canary"))
    safety_monitor = _mapping(safety.get("monitor"))
    safety_limits = _mapping(safety_monitor.get("limits"))
    expected_cgroup_limits = _mapping(safety_limits.get("expected_cgroup_limits"))
    backend_scope = _mapping(safety_monitor.get("backend_scope"))
    feature = _mapping(sections.get("feature_probe"))
    feature_child = _mapping(feature.get("child"))
    feature_validation = _mapping(feature.get("validation"))
    dependency_preflight = _mapping(sections.get("dependency_preflight"))
    model_safety = _mapping(dependency_preflight.get("model_safety"))
    feature_dependencies = _mapping(dependency_preflight.get("feature_checkpoint"))
    source_dependency_contracts = _mapping(
        dependency_preflight.get("source_dependency_contracts")
    )
    official = _mapping(sections.get("official_templates"))
    official_child = _mapping(official.get("child"))
    official_validation = _mapping(official.get("validation"))
    official_artifacts = _rows(official.get("artifacts"))
    official_input_cleanup = _rows(official.get("input_cleanup_validations"))
    official_final_model_safety = _rows(official.get("final_model_safety_validations"))
    custom = _mapping(sections.get("custom_workflow"))
    custom_artifacts = _rows(custom.get("artifacts"))
    ai_agent = _mapping(sections.get("ai_agent_generation"))
    ai_artifacts = _rows(ai_agent.get("artifacts"))
    workflow_ui = _mapping(sections.get("workflow_ui"))
    ui_rows = _rows(workflow_ui.get("rows"))
    offline = _mapping(sections.get("offline_failure"))
    cleanup = _mapping(probe.get("cleanup"))

    report_entry = _mapping(artifact_index.get("report"))
    report_path = Path(str(report_entry.get("path") or "")).expanduser()
    artifact_root = report_path.parent.resolve(strict=False)
    try:
        indexed_report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        indexed_report_payload = None
    indexed_rows = _rows(artifact_index.get("artifacts"))
    indexed_paths: set[str] = set()
    indexed_relative_paths: set[str] = set()
    index_rows_valid = bool(indexed_rows)
    for row in indexed_rows:
        path = Path(str(row.get("path") or "")).expanduser()
        relative = str(row.get("relative_path") or "")
        try:
            resolved = path.resolve(strict=True)
            relative_path = resolved.relative_to(artifact_root).as_posix()
            valid = bool(
                path.is_absolute()
                and path.is_file()
                and not path.is_symlink()
                and relative
                and relative == relative_path
                and int(row.get("size_bytes") or 0) == resolved.stat().st_size
                and str(row.get("sha256") or "") == _sha256_file(resolved)
                and _structured_file_parses(resolved)
                and relative not in indexed_relative_paths
                and str(resolved) not in indexed_paths
            )
        except (OSError, RuntimeError, ValueError):
            resolved = path.resolve(strict=False)
            valid = False
        index_rows_valid = index_rows_valid and valid
        indexed_paths.add(str(resolved))
        indexed_relative_paths.add(relative)

    report_recorded_in_rows = str(report_path.resolve(strict=False)) in indexed_paths
    artifact_index_valid = bool(
        artifact_index.get("schema_version")
        == "hackme.formal-comfyui-workflows-artifact-index/v1"
        and str(artifact_index.get("run_id") or "")
        == str(probe.get("run_id") or "")
        and int(artifact_index.get("artifact_count") or -1) == len(indexed_rows)
        and index_rows_valid
        and report_recorded_in_rows
        and isinstance(indexed_report_payload, Mapping)
        and dict(indexed_report_payload) == dict(probe)
        and _readable_regular_file(report_path)
        and int(report_entry.get("size_bytes") or 0) == report_path.stat().st_size
        and str(report_entry.get("sha256") or "") == _sha256_file(report_path)
    )

    required_artifact_paths: set[str] = set()
    safe_canary_artifacts = _rows(safe_canary.get("artifacts"))
    for row in [*safe_canary_artifacts, *official_artifacts, *custom_artifacts, *ai_artifacts]:
        path = Path(str(row.get("path") or "")).expanduser().resolve(strict=False)
        required_artifact_paths.add(str(path))
    for row in ui_rows:
        required_artifact_paths.update({
            str(Path(str(row.get("main_screenshot") or "")).expanduser().resolve(strict=False)),
            str(Path(str(row.get("editor_screenshot") or "")).expanduser().resolve(strict=False)),
        })
    offline_screenshot = Path(str(offline.get("ui_screenshot") or "")).expanduser().resolve(strict=False)
    required_artifact_paths.add(str(offline_screenshot))
    required_artifacts_indexed = bool(
        required_artifact_paths
        and required_artifact_paths.issubset(indexed_paths)
        and all(_readable_regular_file(path) for path in required_artifact_paths)
    )

    required_nodes = {
        "CheckpointLoaderSimple",
        "KSampler",
        "VAEDecode",
        "SaveImage",
    }
    safe_required_nodes = {
        "UnetLoaderGGUF",
        "DualCLIPLoader",
        "CLIPTextEncode",
        "EmptyLatentImage",
        "VAELoader",
    }
    safe_key = (
        str(safe_selection.get("profile_id") or ""),
        str(safe_selection.get("variant_id") or ""),
    )
    safe_size = FORMAL_SAFE_GGUF_ALLOWLIST.get(safe_key)
    safe_companions = FORMAL_SAFE_GGUF_COMPANIONS.get(safe_key) or {}
    actual_files = _mapping(safe_selection.get("actual_files"))
    expected_safe_files = {
        "gguf_file": (str(safe_selection.get("gguf_file") or ""), int(safe_size or 0)),
        **safe_companions,
    }
    safe_actual_files_ok = bool(
        safe_size is not None
        and set(actual_files) == set(expected_safe_files)
        and all(
            str(_mapping(actual_files.get(slot)).get("relative_path") or "").endswith(filename)
            and int(_mapping(actual_files.get(slot)).get("size_bytes") or -1) == expected_size
            and len(str(_mapping(actual_files.get(slot)).get("sha256") or "")) == 64
            and _file_size_matches(_mapping(actual_files.get(slot)).get("path"), expected_size)
            for slot, (filename, expected_size) in expected_safe_files.items()
        )
    )
    expected_scope = str(backend_scope.get("campaign_cgroup_path") or "")
    backend_scope_ok = bool(
        backend_scope.get("ok") is True
        and int(backend_scope.get("backend_pid") or 0) > 1
        and int(backend_scope.get("backend_start_ticks") or 0) > 0
        and backend_scope.get("backend_inside_campaign_scope") is True
        and int(backend_scope.get("probe_pid") or 0) > 1
        and backend_scope.get("probe_inside_campaign_scope") is True
        and expected_scope.startswith("/")
        and expected_scope != "/"
        and (
            str(backend_scope.get("backend_cgroup_path") or "") == expected_scope
            or str(backend_scope.get("backend_cgroup_path") or "").startswith(expected_scope.rstrip("/") + "/")
        )
        and (
            str(backend_scope.get("probe_cgroup_path") or "") == expected_scope
            or str(backend_scope.get("probe_cgroup_path") or "").startswith(expected_scope.rstrip("/") + "/")
        )
        and len(str(backend_scope.get("backend_cmdline_sha256") or "")) == 64
        and backend_scope.get("models_root_bound_to_backend") is True
        and Path(str(backend_scope.get("models_root") or "")).resolve(strict=False)
        == (Path(str(backend_scope.get("backend_cwd") or "")).resolve(strict=False) / "models").resolve(strict=False)
        and int(backend_scope.get("backend_port") or 0) > 0
        and backend_scope.get("listening_socket_verified") is True
        and bool(list(backend_scope.get("matching_listening_socket_inodes") or []))
    )
    safety_ok = bool(
        safety.get("ok") is True
        and safe_size is not None
        and int(safe_selection.get("size_bytes") or 0) == safe_size
        and safe_selection.get("size_evidence") == "versioned_allowlist_plus_actual_file_stat_and_sha256"
        and safe_selection.get("remote_file_stat_available") is True
        and int(safe_selection.get("max_size_bytes") or 0) == 2 * 1024**3
        and int(safe_selection.get("max_workflow_model_total_bytes") or 0) == 4 * 1024**3
        and int(safe_selection.get("actual_model_total_bytes") or 0)
        == sum(size for _filename, size in expected_safe_files.values())
        and safe_actual_files_ok
        and str(safe_selection.get("safe_vae_override") or "")
        == safe_companions.get("vae_name", ("", 0))[0]
        and str(safe_selection.get("backend_url") or "").rstrip("/")
        == str(probe.get("comfyui_url") or "").rstrip("/")
        and safe_selection.get("selection_rule") == "first_exact_match_in_versioned_allowlist"
        and safe_canary.get("ok") is True
        and safe_canary.get("workflow_id") == "origin_sdxl_gguf_txt2img"
        and safe_canary.get("terminal_status") == "completed"
        and str(safe_canary.get("profile_id") or "") == safe_key[0]
        and str(safe_canary.get("variant_id") or "") == safe_key[1]
        and int(safe_canary.get("size_bytes") or 0) == safe_size
        and str(safe_canary.get("safe_vae_override") or "")
        == safe_companions.get("vae_name", ("", 0))[0]
        and int(safe_canary.get("workflow_run_id") or 0) > 0
        and int(safe_canary.get("terminal_workflow_run_id") or 0)
        == int(safe_canary.get("workflow_run_id") or 0)
        and str(safe_canary.get("job_id") or "")
        and int(safe_canary.get("artifact_count") or 0) == len(safe_canary_artifacts)
        and bool(safe_canary_artifacts)
        and all(_output_artifact_valid(row) for row in safe_canary_artifacts)
        and _workflow_input_cleanup_validation_valid(
            safe_canary.get("input_cleanup_validation")
        )
        and _final_model_safety_validation_valid(
            safe_canary.get("final_model_safety_validation")
        )
        and safety_monitor.get("sample_schema_version")
        == "hackme.formal-comfyui-safety-sample/v1"
        and int(safety_limits.get("min_mem_available_bytes") or 0) >= 1024 * 1024 * 1024
        and int(safety_limits.get("min_disk_free_bytes") or 0) >= 20 * 1024 * 1024 * 1024
        and int(safety_limits.get("max_queue_depth") if safety_limits.get("max_queue_depth") is not None else 99) <= 1
        and 15 <= int(safety_limits.get("cancel_grace_seconds") or 0) <= 60
        and int(safety_limits.get("min_backend_vram_free_bytes") or 0) == 256 * 1024**2
        and int(safety_limits.get("max_gpu_temperature_c") or 0) == 80
        and dict(expected_cgroup_limits) == EXPECTED_COMFY_CGROUP_LIMITS
        and backend_scope_ok
        and int(safety_monitor.get("sample_count") or 0) >= 3
        and safety_monitor.get("samples_complete") is True
        and float(safety_monitor.get("field_completeness_ratio") or 0) == 1.0
        and not list(safety_monitor.get("collector_errors") or [])
        and not list(safety_monitor.get("hard_stop_samples") or [])
        and safety_monitor.get("sample_gap_within_30_seconds") is True
        and all(row.get("ok") is True for row in _rows(safety_monitor.get("abort_events")))
        and _safety_samples_valid(safety_monitor.get("sample_path"), expected_scope=expected_scope)
    )
    real_backend_ok = bool(
        real_backend.get("ok") is True
        and _mapping(real_backend.get("health")).get("ok") is True
        and int(real_backend.get("object_info_node_count") or 0) >= len(required_nodes)
        and set(real_backend.get("required_nodes") or []) == required_nodes
        and not list(real_backend.get("missing_nodes") or [])
        and set(real_backend.get("safe_required_nodes") or []) == safe_required_nodes
        and not list(real_backend.get("missing_safe_nodes") or [])
        and safety_ok
        and str(probe.get("comfyui_url") or "").startswith(("http://", "https://"))
    )

    feature_checkpoint = _mapping(feature_dependencies.get("checkpoint"))
    feature_upscale = _mapping(feature_dependencies.get("upscale_model"))
    feature_controlnet = _mapping(feature_dependencies.get("controlnet"))
    feature_files = (feature_checkpoint, feature_upscale, feature_controlnet)
    feature_dependencies_ok = bool(
        feature_dependencies.get("ok") is True
        and feature_dependencies.get("selection_rule")
        == "all_explicit_exact_inventory_actual_stat_sha256_no_fallback"
        and all(
            int(row.get("size_bytes") or 0) > 0
            and int(row.get("size_bytes") or 0) <= 2 * 1024**3
            and len(str(row.get("sha256") or "")) == 64
            and _file_size_matches(row.get("path"), int(row.get("size_bytes") or 0))
            for row in feature_files
        )
        and str(feature_checkpoint.get("checkpoint") or "")
        and str(feature_upscale.get("name") or "")
        and str(feature_controlnet.get("type") or "")
        and str(feature_controlnet.get("model_name") or "")
        and str(feature_controlnet.get("preprocessor") or "")
        and int(feature_dependencies.get("actual_model_total_bytes") or 0)
        == sum(int(row.get("size_bytes") or 0) for row in feature_files)
        and int(feature_dependencies.get("actual_model_total_bytes") or 0) <= 4 * 1024**3
        and int(feature_dependencies.get("max_file_bytes") or 0) == 2 * 1024**3
        and int(feature_dependencies.get("max_total_bytes") or 0) == 4 * 1024**3
    )
    feature_cleanup = _mapping(feature.get("input_cleanup"))
    feature_history_ids = list(feature.get("created_history_ids") or [])
    feature_ok = bool(
        feature.get("ok") is True
        and int(feature_child.get("exit_code") if feature_child.get("exit_code") is not None else -1) == 0
        and feature_validation.get("ok") is True
        and not list(feature_validation.get("missing") or [])
        and not list(feature_validation.get("duplicates") or [])
        and not dict(feature_validation.get("non_pass") or {})
        and int(feature.get("decoded_output_count") or 0) >= 6
        and feature.get("history_inventory_exact") is True
        and len(feature_history_ids) == 7
        and all(isinstance(item, int) and item > 0 for item in feature_history_ids)
        and len(set(feature_history_ids)) == len(feature_history_ids)
        and feature_dependencies_ok
        and _mapping(feature.get("feature_checkpoint")) == feature_dependencies
        and feature_cleanup.get("exact") is True
        and int(feature_cleanup.get("attempted_count") or 0) > 0
        and int(feature_cleanup.get("exact_deleted_or_missing_count") or -1)
        == int(feature_cleanup.get("attempted_count") or 0)
        and len(_rows(feature_cleanup.get("rows")))
        == int(feature_cleanup.get("attempted_count") or 0)
        and not list(feature_cleanup.get("immutable_residuals") or [])
        and not list(feature_cleanup.get("uncertain_uploads") or [])
        and not list(feature_cleanup.get("failures") or [])
        and all(
            row.get("correlated") is True
            and row.get("exact") is True
            and row.get("immutable_residual") is False
            and int(_mapping(row.get("response")).get("_http_status") or 0) == 200
            and _mapping(row.get("response")).get("ok") is True
            and _exact_discard_receipt_valid(
                _mapping(row.get("response")).get("discard")
            )
            for row in _rows(feature_cleanup.get("rows"))
        )
        and _readable_regular_file(feature.get("report_path"))
    )

    model_safety_limits = _mapping(model_safety.get("limits"))
    model_safety_rows = _mapping(model_safety.get("workflows"))
    model_safety_ok = bool(
        model_safety.get("schema_version") == "hackme.formal-comfyui-model-safety/v1"
        and model_safety.get("ok") is True
        and int(model_safety.get("expected_workflow_count") or 0) == len(expected_workflows)
        and int(model_safety.get("actual_workflow_count") or 0) == len(expected_workflows)
        and int(model_safety.get("safe_workflow_count") or 0) == len(expected_workflows)
        and int(
            model_safety.get("unsafe_workflow_count")
            if model_safety.get("unsafe_workflow_count") is not None
            else -1
        ) == 0
        and not list(model_safety.get("unsafe_workflows") or [])
        and model_safety.get("hash_coverage_complete") is True
        and int(model_safety_limits.get("max_model_file_bytes") or 0) == 2 * 1024**3
        and int(model_safety_limits.get("max_workflow_model_total_bytes") or 0) == 4 * 1024**3
        and model_safety_limits.get("limits_can_only_tighten") is True
        and set(model_safety_rows) == expected_workflows
        and all(
            _mapping(row).get("ok") is True
            and int(_mapping(row).get("reference_count") or 0) > 0
            and int(_mapping(row).get("model_file_count") or 0) > 0
            and int(_mapping(row).get("model_total_bytes") or 0) > 0
            and int(_mapping(row).get("model_total_bytes") or 0) <= 4 * 1024**3
            and len(_rows(_mapping(row).get("models")))
            == int(_mapping(row).get("model_file_count") or 0)
            and sum(
                int(model.get("size_bytes") or 0)
                for model in _rows(_mapping(row).get("models"))
            ) == int(_mapping(row).get("model_total_bytes") or 0)
            and sum(
                len(_rows(model.get("references")))
                for model in _rows(_mapping(row).get("models"))
            ) == int(_mapping(row).get("reference_count") or 0)
            and not list(_mapping(row).get("errors") or [])
            and not list(_mapping(row).get("oversized_model_files") or [])
            and not list(_mapping(row).get("reasons") or [])
            and all(
                int(model.get("size_bytes") or 0) > 0
                and int(model.get("size_bytes") or 0) <= 2 * 1024**3
                and len(str(model.get("sha256") or "")) == 64
                and _file_size_matches(model.get("path"), int(model.get("size_bytes") or 0))
                and bool(_rows(model.get("references")))
                for model in _rows(_mapping(row).get("models"))
            )
            for row in model_safety_rows.values()
        )
    )

    official_bundle_ids = [str(row.get("bundle_id") or "") for row in official_artifacts]
    source_dependency_contracts_ok = bool(
        dependency_preflight.get("source_dependency_contracts_ok") is True
        and int(dependency_preflight.get("source_dependency_contract_count") or 0)
        == len(expected_workflows)
        and set(source_dependency_contracts) == expected_workflows
        and all(
            _manifest_dependency_contract_valid(contract)
            for contract in source_dependency_contracts.values()
        )
    )
    official_ok = bool(
        dependency_preflight.get("ok") is True
        and int(dependency_preflight.get("expected_count") or 0) == len(expected_workflows)
        and int(dependency_preflight.get("actual_count") or 0) == len(expected_workflows)
        and not list(dependency_preflight.get("missing_workflows") or [])
        and not list(dependency_preflight.get("unexpected_workflows") or [])
        and not dict(dependency_preflight.get("dependency_failures") or {})
        and source_dependency_contracts_ok
        and dependency_preflight.get("safe_override_ok") is True
        and model_safety_ok
        and feature_dependencies_ok
        and str(dependency_preflight.get("safe_profile_id") or "") == safe_key[0]
        and str(dependency_preflight.get("safe_variant_id") or "") == safe_key[1]
        and _readable_regular_file(dependency_preflight.get("all_report_path"))
        and _readable_regular_file(dependency_preflight.get("safe_report_path"))
        and official.get("ok") is True
        and int(official_child.get("exit_code") if official_child.get("exit_code") is not None else -1) == 0
        and official_validation.get("ok") is True
        and int(official_validation.get("expected_count") or 0) == len(expected_workflows)
        and int(official_validation.get("actual_count") or 0) == len(expected_workflows)
        and not list(official_validation.get("missing") or [])
        and not list(official_validation.get("unexpected") or [])
        and not list(official_validation.get("duplicates") or [])
        and not dict(official_validation.get("bad_status") or {})
        and official_validation.get("exact_counts") is True
        and official_validation.get("connection_ok") is True
        and int(official_validation.get("error_console_count") or 0) == 0
        and int(official_validation.get("page_error_count") or 0) == 0
        and int(official_validation.get("network_error_count") or 0) == 0
        and int(official.get("artifact_count") or 0) == len(official_artifacts)
        and len(official_artifacts) >= len(expected_workflows)
        and set(official_bundle_ids) == expected_workflows
        and all(_output_artifact_valid(row) for row in official_artifacts)
        and int(official.get("input_cleanup_validated_count") or 0)
        == len(expected_workflows)
        and len(official_input_cleanup) == len(expected_workflows)
        and {
            str(row.get("bundle_id") or "")
            for row in official_input_cleanup
        } == expected_workflows
        and all(
            _workflow_input_cleanup_validation_valid(row)
            for row in official_input_cleanup
        )
        and int(official.get("final_model_safety_validated_count") or 0)
        == len(expected_workflows)
        and len(official_final_model_safety) == len(expected_workflows)
        and {
            str(row.get("bundle_id") or "")
            for row in official_final_model_safety
        } == expected_workflows
        and all(
            _final_model_safety_validation_valid(row)
            for row in official_final_model_safety
        )
        and _readable_regular_file(official.get("report_path"))
    )

    custom_ok = bool(
        custom.get("ok") is True
        and int(custom.get("preset_id") or 0) > 0
        and int(custom.get("workflow_run_id") or 0) > 0
        and str(custom.get("job_id") or "")
        and custom.get("terminal_status") == "completed"
        and str(custom.get("safe_profile_id") or "") == safe_key[0]
        and str(custom.get("safe_variant_id") or "") == safe_key[1]
        and str(custom.get("safe_gguf_file") or "") == str(safe_selection.get("gguf_file") or "")
        and str(custom.get("safe_vae_override") or "") == str(safe_selection.get("safe_vae_override") or "")
        and len(str(custom.get("workflow_sha256") or "")) == 64
        and int(custom.get("artifact_count") or 0) == len(custom_artifacts)
        and bool(custom_artifacts)
        and all(_output_artifact_valid(row) for row in custom_artifacts)
        and _workflow_input_cleanup_validation_valid(
            custom.get("input_cleanup_validation")
        )
        and _final_model_safety_validation_valid(
            custom.get("final_model_safety_validation")
        )
        and _mapping(custom.get("delete")).get("ok") is True
        and int(custom.get("delete_verified_http_status") or 0) in {403, 404}
    )

    ai_agent_ok = bool(
        ai_agent.get("ok") is True
        and list(ai_agent.get("catalog_names") or []) == ["write_comfyui_generate"]
        and str(ai_agent.get("job_id") or "")
        and int(ai_agent.get("workflow_run_id") or 0) > 0
        and int(ai_agent.get("history_id") or 0) == 0
        and ai_agent.get("official_workflow_id") == "origin_sdxl_gguf_txt2img"
        and str(ai_agent.get("safe_profile_id") or "") == safe_key[0]
        and str(ai_agent.get("safe_variant_id") or "") == safe_key[1]
        and str(ai_agent.get("safe_gguf_file") or "") == str(safe_selection.get("gguf_file") or "")
        and str(ai_agent.get("safe_vae_override") or "") == str(safe_selection.get("safe_vae_override") or "")
        and ai_agent.get("terminal_status") == "completed"
        and int(ai_agent.get("artifact_count") or 0) == len(ai_artifacts)
        and bool(ai_artifacts)
        and all(_output_artifact_valid(row) for row in ai_artifacts)
        and _workflow_input_cleanup_validation_valid(
            ai_agent.get("input_cleanup_validation")
        )
        and _final_model_safety_validation_valid(
            ai_agent.get("final_model_safety_validation")
        )
        and bool(_mapping(ai_agent.get("action_policy")))
    )

    ui_labels = [str(row.get("label") or "") for row in ui_rows]
    workflow_ui_ok = bool(
        workflow_ui.get("ok") is True
        and workflow_ui.get("browser_closed") is True
        and len(ui_rows) == 2
        and set(ui_labels) == {"desktop", "mobile"}
        and len(set(ui_labels)) == len(ui_labels)
        and all(
            row.get("ok") is True
            and row.get("context_closed") is True
            and int(_mapping(row.get("main")).get("options") or 0) > 1
            and int(_mapping(row.get("main")).get("official") or 0) == len(expected_workflows)
            and _mapping(row.get("main")).get("visualLinkVisible") is True
            and int(_mapping(row.get("editor")).get("nodes") or 0) >= 7
            and int(_mapping(row.get("editor")).get("edges") or 0) >= 8
            and row.get("overflow") is False
            and row.get("editor_overflow") is False
            and not list(row.get("console_errors") or [])
            and not list(row.get("page_errors") or [])
            and not list(row.get("failed_requests") or [])
            and _image_file_decodes(Path(str(row.get("main_screenshot") or "")))
            and _image_file_decodes(Path(str(row.get("editor_screenshot") or "")))
            for row in ui_rows
        )
    )

    offline_status = _mapping(offline.get("status"))
    offline_generation = _mapping(offline.get("generation"))
    terminal_failure = _mapping(offline.get("terminal_failure"))
    synchronous_failure = bool(
        int(offline.get("generation_http") or 0) >= 400
        and offline_generation.get("ok") is False
    )
    asynchronous_failure = str(terminal_failure.get("status") or "").lower() in {
        "error",
        "failed",
        "cancelled",
    }
    status_text = str(offline.get("ui_status_text") or "").lower()
    offline_ok = bool(
        offline.get("ok") is True
        and int(offline.get("status_http") or 0) == 200
        and offline_status.get("available") is False
        and int(offline.get("workflows_http") or 0) == 200
        and str(offline.get("dependency_warning") or "").strip()
        and (synchronous_failure or asynchronous_failure)
        and status_text
        and any(token in status_text for token in ("失敗", "無法", "未連線", "錯誤", "offline", "error", "failed"))
        and not list(offline.get("page_errors") or [])
        and offline.get("restored_available") is True
        and offline.get("browser_closed") is True
        and _image_file_decodes(offline_screenshot)
    )

    history_cleanup = _mapping(cleanup.get("history"))
    workflow_cleanup = _mapping(cleanup.get("workflow_inventory"))
    settings_cleanup = _mapping(cleanup.get("settings_restore"))
    final_safety_cleanup = _mapping(cleanup.get("safety_final"))
    discard_rows = _rows(cleanup.get("discard"))
    allowlist_rows = _rows(cleanup.get("retained_remote_output_allowlist"))
    exact_cleanup = bool(
        cleanup.get("exact") is True
        and "discard_error" not in cleanup
        and history_cleanup.get("ok") is True
        and history_cleanup.get("exact") is True
        and list(history_cleanup.get("baseline_ids") or [])
        == list(history_cleanup.get("after_ids") or [])
        and workflow_cleanup.get("ok") is True
        and list(workflow_cleanup.get("baseline_ids") or [])
        == list(workflow_cleanup.get("after_ids") or [])
        and not list(workflow_cleanup.get("unexpected") or [])
        and not list(workflow_cleanup.get("missing") or [])
        and settings_cleanup.get("ok") is True
        and settings_cleanup.get("exact") is True
        and not list(settings_cleanup.get("errors") or [])
        and final_safety_cleanup.get("ok") is True
        and final_safety_cleanup.get("queue_empty_verified") is True
        and bool(discard_rows)
        and not allowlist_rows
        and all(
            row.get("ok") is True
            and int(row.get("http_status") or 0) == 200
            and not str(row.get("warning") or "")
            and _exact_discard_receipt_valid(row.get("discard"))
            for row in discard_rows
        )
    )

    evidence = {
        "real_backend_required": real_backend_ok,
        "feature_probe": feature_ok,
        "official_templates_execute": official_ok,
        "custom_workflow_create_import_run_output_delete": custom_ok,
        "ai_agent_generation_terminal_output": ai_agent_ok,
        "desktop_mobile_workflow_ui": workflow_ui_ok,
        "offline_and_dependency_failure_visible": offline_ok,
    }
    contract = _mapping(probe.get("contract"))
    strict_probe_terminal = bool(
        probe.get("schema_version") == "hackme.formal-comfyui-workflows-probe/v1"
        and str(probe.get("run_id") or "")
        and probe.get("ok") is True
        and isinstance(probe.get("errors"), list)
        and not probe.get("errors")
        and set(contract) == evidence_ids
        and all(type(contract.get(key)) is bool and contract.get(key) is True for key in evidence_ids)
    )
    return {
        "scenario_assertions": evidence,
        "terminal_assertions": {
            "all_domain_assertions_true": all(evidence.values()),
            "strict_probe_contract_pass": strict_probe_terminal,
            "terminal_outputs_backend_restored_and_index_verified": bool(
                official_ok
                and custom_ok
                and ai_agent_ok
                and offline_ok
                and artifact_index_valid
                and required_artifacts_indexed
            ),
        },
            "cleanup_assertions": {
                "exact_history_workflow_and_settings_restore": exact_cleanup,
                "generated_previews_discarded_or_remote_allowlisted": bool(
                    exact_cleanup
                ),
            "browser_contexts_closed_and_artifacts_readable": bool(
                workflow_ui_ok
                and offline.get("browser_closed") is True
                and artifact_index_valid
                and required_artifacts_indexed
            ),
        },
        "details": {
            "source_count": len(indexed_rows) + 1,
            "official_workflow_count": len(expected_workflows),
            "official_output_count": len(official_artifacts),
            "indexed_artifact_count": len(indexed_rows),
        },
    }


__all__ = [
    "backup_restore_assertions",
    "ai_agent_positive_assertions",
    "cloud_drive_stream_assertions",
    "comfyui_workflow_assertions",
    "community_governance_assertions",
    "final_ui_assertions",
    "media_long_assertions",
    "media_proxy_assertions",
    "pointschain_hft_assertions",
    "server_emergency_assertions",
    "trading_workflow_assertions",
    "wallet_incident_assertions",
]
