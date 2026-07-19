#!/usr/bin/env python3
"""Fail-closed formal evidence probe for hackme_web + a real ComfyUI.

This probe is intentionally stricter than the developer-facing ComfyUI probes:

* the backend URL must come from ``HACKME_CAMPAIGN_COMFYUI_API_URL``;
* every feature-probe row, every checked-in official workflow, and every
  terminal output must pass (there is no skip/expected-gap state);
* a custom workflow is exported, imported, edited, run, decoded, and deleted;
* ``write_comfyui_generate`` is exercised through the AI Agent write-tool API;
* desktop/mobile workflow UI and an offline dependency failure are observed;
* settings, feature flags, workflow presets, and history rows are restored to
  their exact pre-probe inventory.

Generated outputs are copied under ``--out-dir`` before site-side history and
preview state is discarded.  Every source-file deletion must carry exact
post-delete absence evidence.  A remote backend that cannot prove deletion is
a formal failure; immutable backend outputs are never converted into a pass.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import http.cookiejar
import json
import os
import signal
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.testing.probe_credentials import add_root_password_argument  # noqa: E402
from services.comfyui.client import ComfyUIClient  # noqa: E402
from services.comfyui.template.gguf_workflow import apply_gguf_workflow_profile  # noqa: E402
from services.comfyui.template.seeding import SYSTEM_WORKFLOW_IDS  # noqa: E402
from services.comfyui.workflow.final_model_safety import (  # noqa: E402
    FinalModelSafetyError,
    revalidate_final_model_safety_receipt_files,
)


SCHEMA_VERSION = "hackme.formal-comfyui-workflows-probe/v1"
REQUIRED_FEATURE_ROWS = frozenset({
    "status",
    "models",
    "txt2img",
    "img2img",
    "inpaint",
    "outpaint",
    "upscale",
    "controlnet",
    "history_list",
    "history_rerun",
})
SNAPSHOT_SETTING_KEYS = (
    "comfyui_connection_mode",
    "comfyui_remote_api_url",
    "ai_agent_operation_mode",
    "ai_agent_allowed_tools",
    "module_ai_agent_min_role",
)
SNAPSHOT_FEATURE_KEYS = (
    "feature_comfyui_enabled",
    "feature_ai_agent_enabled",
    "feature_comfyui_template_importer_strict",
)
TERMINAL_JOB_STATES = frozenset({"completed", "error", "failed", "cancelled"})
SAFE_GGUF_WORKFLOW_ID = "origin_sdxl_gguf_txt2img"
SAFETY_SAMPLE_SCHEMA_VERSION = "hackme.formal-comfyui-safety-sample/v1"
SAFE_GGUF_ALLOWLIST = (
    {
        "profile_id": "diving_illustrious_flat_anime_sdxl",
        "variant_id": "q4_k_m",
        "gguf_file": "diving-illustrious-flat-anime-paradigm-shift.Q4_K_M.gguf",
        "size_bytes": 1_446_633_120,
        "companions": {
            "clip_name1": {"filename": "clip_l.safetensors", "size_bytes": 246_144_378},
            "clip_name2": {"filename": "clip_g.safetensors", "size_bytes": 1_389_363_370},
            # This exact VAE completed the live 512x512/2-step canary.  Do not
            # fall back to the checked-in workflow's missing sdxl_vae default.
            "vae_name": {"filename": "illustrious_vae.safetensors", "size_bytes": 167_340_358},
        },
    },
    {
        "profile_id": "calcuis_illustrious_sdxl",
        "variant_id": "q4_0",
        "gguf_file": "illustrious-q4_0.gguf",
        "size_bytes": 1_457_146_848,
        "companions": {
            "clip_name1": {"filename": "illustrious_clip_l.safetensors", "size_bytes": 247_330_924},
            "clip_name2": {"filename": "illustrious_clip_g.safetensors", "size_bytes": 1_389_389_196},
            "vae_name": {"filename": "illustrious_vae.safetensors", "size_bytes": 167_340_358},
        },
    },
)
GIB = 1024 * 1024 * 1024
SAFE_MODEL_MAX_FILE_BYTES = 2 * GIB
SAFE_WORKFLOW_MODEL_TOTAL_BYTES = 4 * GIB
EXPECTED_CGROUP_LIMITS = {
    "memory_high": 5 * GIB,
    "memory_max": 6 * GIB,
    "memory_swap_max": 512 * 1024**2,
    "cpu_quota": 300_000,
    "cpu_period": 100_000,
    "pids_max": 384,
}
MIN_BACKEND_VRAM_FREE_BYTES = 256 * 1024 * 1024
MAX_GPU_TEMPERATURE_C = 80
SAFETY_EXPECTED_FIELDS = frozenset({
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
})


class ProbeFailure(RuntimeError):
    """A formal-contract failure, never an expected gap."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_comfyui_models_root(environ: dict[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    raw = str(env.get("HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT") or "").strip()
    if not raw:
        raise ProbeFailure("HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT is required for actual model stat/hash evidence")
    root = Path(raw).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise ProbeFailure(f"ComfyUI models root is not a real directory: {root}")
    return root


def _normalise_model_name(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ProbeFailure(f"unsafe model path in official workflow: {raw}")
    return candidate.as_posix()


def _model_folders(class_type: str, input_name: str) -> tuple[str, ...]:
    class_lower = str(class_type or "").lower()
    key = str(input_name or "")
    if key == "ckpt_name":
        return ("checkpoints",)
    if key == "unet_name":
        return ("diffusion_models", "unet")
    if key == "vae_name":
        return ("vae",)
    if key == "lora_name":
        return ("loras",)
    if key == "control_net_name":
        return ("controlnet",)
    if key == "text_encoder" or key.startswith("clip_name"):
        if "vision" in class_lower:
            return ("clip_vision",)
        return ("text_encoders", "clip")
    if key == "model_name" and "upscale" in class_lower:
        return ("upscale_models", "latent_upscale_models")
    raise ProbeFailure(f"unsupported loader model input requires an explicit safety rule: {class_type}.{input_name}")


def _resolve_model_file(models_root: Path, class_type: str, input_name: str, value: Any) -> Path:
    relative = _normalise_model_name(value)
    if not relative:
        raise ProbeFailure(f"empty model reference: {class_type}.{input_name}")
    candidates = [models_root / folder / relative for folder in _model_folders(class_type, input_name)]
    candidates.append(models_root / relative)
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        if models_root != resolved and models_root not in resolved.parents:
            raise ProbeFailure(f"model symlink escapes models root: {candidate} -> {resolved}")
        if resolved.is_file():
            return resolved
    raise ProbeFailure(
        f"cannot stat exact model file for {class_type}.{input_name}={relative!r} under {models_root}"
    )


def _workflow_model_references(workflow: dict[str, Any]) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    for node_id, node in sorted(workflow.items(), key=lambda item: str(item[0])):
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "").strip()
        if "loader" not in class_type.lower() or class_type in {"LoadImage", "LoadImageMask", "LoadVideo"}:
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        for input_name, value in inputs.items():
            key = str(input_name or "")
            if key not in {
                "ckpt_name", "unet_name", "vae_name", "lora_name", "control_net_name",
                "model_name", "text_encoder", "clip_name", "clip_name1", "clip_name2", "clip_name3",
            }:
                continue
            if not isinstance(value, str) or not value.strip():
                continue
            references.append({
                "node_id": str(node_id),
                "class_type": class_type,
                "input_name": key,
                "value": _normalise_model_name(value),
            })
    return references


def audit_official_workflow_model_safety(
    models_root: Path,
    *,
    max_file_bytes: int = SAFE_MODEL_MAX_FILE_BYTES,
    max_workflow_total_bytes: int = SAFE_WORKFLOW_MODEL_TOTAL_BYTES,
) -> dict[str, Any]:
    if int(max_file_bytes) > SAFE_MODEL_MAX_FILE_BYTES or int(max_workflow_total_bytes) > SAFE_WORKFLOW_MODEL_TOTAL_BYTES:
        raise ProbeFailure("ComfyUI model safety limits may only be tightened, never weakened")
    rows: dict[str, dict[str, Any]] = {}
    for bundle_id in SYSTEM_WORKFLOW_IDS:
        workflow_path = REPO_ROOT / "workflows" / "comfyui" / bundle_id / "workflow.json"
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        refs = _workflow_model_references(workflow)
        files: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for ref in refs:
            try:
                path = _resolve_model_file(models_root, ref["class_type"], ref["input_name"], ref["value"])
                size = int(path.stat().st_size)
                key = str(path)
                files.setdefault(key, {
                    "path": key,
                    "relative_path": path.relative_to(models_root).as_posix(),
                    "size_bytes": size,
                    "references": [],
                })["references"].append(ref)
            except Exception as exc:
                errors.append(str(exc))
        total = sum(int(item["size_bytes"]) for item in files.values())
        oversized = [
            item["relative_path"]
            for item in files.values()
            if int(item["size_bytes"]) > int(max_file_bytes)
        ]
        reasons = list(errors)
        if oversized:
            reasons.append(f"model_files_exceed_{int(max_file_bytes)}:{oversized}")
        if total > int(max_workflow_total_bytes):
            reasons.append(f"workflow_model_total_exceeds_{int(max_workflow_total_bytes)}:{total}")
        rows[bundle_id] = {
            "ok": not reasons and bool(refs),
            "reference_count": len(refs),
            "model_file_count": len(files),
            "model_total_bytes": total,
            "models": sorted(files.values(), key=lambda item: item["relative_path"]),
            "errors": errors,
            "oversized_model_files": oversized,
            "reasons": reasons or ([] if refs else ["no_model_references_detected"]),
        }
        if not refs:
            rows[bundle_id]["ok"] = False

    unsafe = {bundle_id: row for bundle_id, row in rows.items() if row.get("ok") is not True}
    # Hashing multi-gigabyte known-unsafe defaults only adds I/O pressure.  We
    # stat everything first and hash every exact model only when the complete
    # all-workflow plan is already within the immutable safety caps.
    if not unsafe:
        hash_cache: dict[str, str] = {}
        for row in rows.values():
            for item in row["models"]:
                hash_cache.setdefault(item["path"], sha256_file(Path(item["path"])))
                item["sha256"] = hash_cache[item["path"]]
    return {
        "schema_version": "hackme.formal-comfyui-model-safety/v1",
        "ok": not unsafe,
        "models_root": str(models_root),
        "models_root_realpath": str(models_root.resolve(strict=True)),
        "limits": {
            "max_model_file_bytes": int(max_file_bytes),
            "max_workflow_model_total_bytes": int(max_workflow_total_bytes),
            "limits_can_only_tighten": True,
        },
        "expected_workflow_count": len(SYSTEM_WORKFLOW_IDS),
        "actual_workflow_count": len(rows),
        "safe_workflow_count": len(rows) - len(unsafe),
        "unsafe_workflow_count": len(unsafe),
        "unsafe_workflows": sorted(unsafe),
        "hash_coverage_complete": not unsafe,
        "workflows": rows,
    }


def _parse_key_value_file(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            values[str(parts[0])] = int(parts[1])
        except (TypeError, ValueError):
            continue
    return values


def _read_limit_value(path: Path) -> int | str:
    value = path.read_text(encoding="utf-8").strip()
    if value == "max":
        return value
    return int(value)


def _current_cgroup_path() -> tuple[str, Path]:
    raw_path = ""
    for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
        if line.startswith("0::"):
            raw_path = line.split("::", 1)[1].strip()
            break
    if not raw_path:
        raise ProbeFailure("ComfyUI safety collector requires cgroup v2 membership")
    root = Path("/sys/fs/cgroup").resolve()
    resolved = (root / raw_path.lstrip("/")).resolve()
    if resolved != root and root not in resolved.parents:
        raise ProbeFailure(f"invalid cgroup path: {raw_path}")
    return raw_path, resolved


def _normalise_cgroup_path(value: str) -> str:
    path = "/" + str(value or "").strip().lstrip("/")
    path = path.rstrip("/") or "/"
    if "/../" in f"{path}/" or "/./" in f"{path}/":
        raise ProbeFailure(f"invalid campaign cgroup path: {value!r}")
    return path


def _pid_cgroup_path(pid: int) -> str:
    raw = Path(f"/proc/{int(pid)}/cgroup").read_text(encoding="utf-8")
    for line in raw.splitlines():
        if line.startswith("0::"):
            return _normalise_cgroup_path(line.split("::", 1)[1])
    raise ProbeFailure(f"pid {pid} has no cgroup v2 membership")


def _pid_start_ticks(pid: int) -> int:
    raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
    closing = raw.rfind(")")
    fields = raw[closing + 2:].split() if closing >= 0 else []
    # The tail starts at field 3 (state), so Linux stat field 22
    # (starttime) is index 19.  Splitting the whole line is unsafe because
    # the parenthesized comm field may itself contain spaces.
    if len(fields) <= 19:
        raise ProbeFailure(f"pid {pid} stat is truncated")
    return int(fields[19])


def _process_tree_metrics(root_pid: int) -> dict[str, Any]:
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            closing = raw.rfind(")")
            fields = raw[closing + 2:].split() if closing >= 0 else []
            if len(fields) <= 1:
                continue
            parents[int(entry.name)] = int(fields[1])
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, OSError):
            continue
    root_pid = int(root_pid)
    if root_pid not in parents:
        raise ProbeFailure(f"backend pid {root_pid} vanished during process-tree collection")
    tree = {root_pid}
    while True:
        discovered = {
            pid for pid, parent in parents.items()
            if parent in tree and pid not in tree
        }
        if not discovered:
            break
        tree.update(discovered)
    rss_bytes = 0
    threads = 0
    fd_count = 0
    observed: list[int] = []
    for pid in sorted(tree):
        try:
            status: dict[str, str] = {}
            for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition(":")
                if separator:
                    status[key] = value.strip()
            rss_kib = int(str(status.get("VmRSS") or "0").split()[0])
            process_threads = int(str(status.get("Threads") or "0").split()[0])
            process_fds = sum(1 for _entry in Path(f"/proc/{pid}/fd").iterdir())
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, OSError):
            if pid == root_pid:
                raise ProbeFailure(f"backend pid {root_pid} vanished during process-tree metrics")
            continue
        observed.append(pid)
        rss_bytes += rss_kib * 1024
        threads += process_threads
        fd_count += process_fds
    if root_pid not in observed:
        raise ProbeFailure(f"backend pid {root_pid} was not observed in process-tree metrics")
    return {
        "pids": observed,
        "rss_bytes": rss_bytes,
        "threads": threads,
        "fd_count": fd_count,
    }


def _listening_socket_inodes(port: int) -> set[str]:
    target_port = f"{int(port):04X}"
    inodes: set[str] = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        for line in table.read_text(encoding="utf-8").splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10:
                continue
            local_address = fields[1]
            state = fields[3]
            if state == "0A" and local_address.rsplit(":", 1)[-1].upper() == target_port:
                inodes.add(fields[9])
    return inodes


def _pid_socket_inodes(pid: int) -> set[str]:
    inodes: set[str] = set()
    for fd in Path(f"/proc/{int(pid)}/fd").iterdir():
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            inodes.add(target[8:-1])
    return inodes


def _cgroup_within(actual: str, expected_parent: str) -> bool:
    actual_norm = _normalise_cgroup_path(actual)
    expected_norm = _normalise_cgroup_path(expected_parent)
    return actual_norm == expected_norm or actual_norm.startswith(expected_norm.rstrip("/") + "/")


def require_backend_scope_evidence(
    comfyui_url: str,
    *,
    models_root: Path,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    parsed = urllib.parse.urlsplit(comfyui_url)
    if str(parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise ProbeFailure(
            "formal ComfyUI safety requires a locally observable backend PID/cgroup; "
            f"remote backend {parsed.hostname!r} has no machine-verifiable hard-limit evidence"
        )
    expected = _normalise_cgroup_path(str(env.get("HACKME_CAMPAIGN_CGROUP_PATH") or ""))
    if expected == "/":
        raise ProbeFailure("HACKME_CAMPAIGN_CGROUP_PATH is required and cannot be the root cgroup")
    try:
        backend_pid = int(str(env.get("HACKME_CAMPAIGN_COMFYUI_BACKEND_PID") or "0"))
    except ValueError as exc:
        raise ProbeFailure("HACKME_CAMPAIGN_COMFYUI_BACKEND_PID is invalid") from exc
    if backend_pid <= 1:
        raise ProbeFailure("HACKME_CAMPAIGN_COMFYUI_BACKEND_PID is required")
    backend_cgroup = _pid_cgroup_path(backend_pid)
    probe_cgroup, _ = _current_cgroup_path()
    backend_inside = _cgroup_within(backend_cgroup, expected)
    probe_inside = _cgroup_within(probe_cgroup, expected)
    if not backend_inside or not probe_inside:
        raise ProbeFailure(
            "ComfyUI backend and formal probe must both be inside the campaign cgroup: "
            f"expected={expected}, backend={backend_cgroup}, probe={probe_cgroup}"
        )
    cmdline = Path(f"/proc/{backend_pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
        "utf-8", errors="replace"
    ).strip()
    if not cmdline:
        raise ProbeFailure(f"ComfyUI backend pid {backend_pid} has an empty command line")
    backend_cwd = Path(f"/proc/{backend_pid}/cwd").resolve(strict=True)
    resolved_models_root = Path(models_root).resolve(strict=True)
    bound_models_root = (backend_cwd / "models").resolve(strict=False)
    models_root_bound = bool(
        resolved_models_root.name == "models"
        and bound_models_root == resolved_models_root
    )
    if not models_root_bound:
        raise ProbeFailure(
            "formal ComfyUI models root is not machine-bound to the listening backend cwd: "
            f"backend_cwd={backend_cwd}, expected={bound_models_root}, configured={resolved_models_root}"
        )
    backend_port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    listening_inodes = _listening_socket_inodes(backend_port)
    backend_socket_inodes = _pid_socket_inodes(backend_pid)
    matching_socket_inodes = sorted(listening_inodes & backend_socket_inodes)
    if not matching_socket_inodes:
        raise ProbeFailure(
            f"ComfyUI backend pid {backend_pid} does not own the listening socket for {comfyui_url}"
        )
    return {
        "ok": True,
        "backend_pid": backend_pid,
        "backend_start_ticks": _pid_start_ticks(backend_pid),
        "backend_cgroup_path": backend_cgroup,
        "backend_inside_campaign_scope": backend_inside,
        "probe_pid": os.getpid(),
        "probe_cgroup_path": probe_cgroup,
        "probe_inside_campaign_scope": probe_inside,
        "campaign_cgroup_path": expected,
        "backend_cmdline_sha256": sha256_bytes(cmdline.encode("utf-8")),
        "backend_cwd": str(backend_cwd),
        "models_root": str(resolved_models_root),
        "models_root_bound_to_backend": models_root_bound,
        "backend_port": backend_port,
        "matching_listening_socket_inodes": matching_socket_inodes,
        "listening_socket_verified": True,
    }


def _read_cpu_max(path: Path) -> dict[str, int | str]:
    parts = path.read_text(encoding="utf-8").strip().split()
    if len(parts) != 2:
        raise ProbeFailure(f"invalid cpu.max: {parts}")
    quota: int | str = parts[0] if parts[0] == "max" else int(parts[0])
    return {"quota": quota, "period": int(parts[1])}


def _read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        name, separator, raw = line.partition(":")
        if not separator:
            continue
        parts = raw.strip().split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        if len(parts) > 1 and parts[1].lower() == "kb":
            value *= 1024
        values[name] = value
    return values


def _read_psi(kind: str) -> dict[str, dict[str, float | int]]:
    result: dict[str, dict[str, float | int]] = {}
    for line in (Path("/proc/pressure") / kind).read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts:
            continue
        row: dict[str, float | int] = {}
        for token in parts[1:]:
            key, separator, raw = token.partition("=")
            if not separator:
                continue
            row[key] = int(raw) if key == "total" else float(raw)
        result[parts[0]] = row
    return result


def _queue_rows(queue: dict[str, Any], name: str) -> list[Any]:
    rows = queue.get(name) if isinstance(queue, dict) else []
    return rows if isinstance(rows, list) else []


def _queue_prompt_ids(queue: dict[str, Any]) -> set[str]:
    prompt_ids: set[str] = set()
    for name in ("queue_running", "queue_pending"):
        for row in _queue_rows(queue, name):
            if isinstance(row, (list, tuple)) and len(row) > 1 and str(row[1]).strip():
                prompt_ids.add(str(row[1]).strip())
    return prompt_ids


class ComfySafetyMonitor:
    """Collect fail-closed backend/host/cgroup evidence and bound cancellation."""

    def __init__(
        self,
        direct: ComfyUIClient,
        *,
        sample_path: Path,
        artifact_root: Path,
        min_mem_available_bytes: int,
        min_disk_free_bytes: int,
        max_queue_depth: int,
        cancel_grace_seconds: int,
        backend_scope: dict[str, Any],
    ):
        self.direct = direct
        self.sample_path = sample_path
        self.artifact_root = artifact_root
        self.min_mem_available_bytes = int(min_mem_available_bytes)
        self.min_disk_free_bytes = int(min_disk_free_bytes)
        self.max_queue_depth = int(max_queue_depth)
        self.cancel_grace_seconds = int(cancel_grace_seconds)
        self.backend_scope = dict(backend_scope)
        self.samples: list[dict[str, Any]] = []
        self.abort_events: list[dict[str, Any]] = []
        self.baseline_queue_ids: set[str] = set()
        self.baseline_memory_events: dict[str, int] | None = None

    def sample(self, phase: str, *, job_id: str = "", allowed_queue_depth: int | None = None) -> dict[str, Any]:
        expected_fields = sorted(SAFETY_EXPECTED_FIELDS)
        valid_fields: list[str] = []
        collector_errors: list[str] = []
        backend: dict[str, Any] = {}
        host: dict[str, Any] = {}
        cgroup: dict[str, Any] = {}

        try:
            queue = self.direct._json_request("/queue", timeout=3)
            running = _queue_rows(queue, "queue_running")
            pending = _queue_rows(queue, "queue_pending")
            backend.update({
                "queue_running": len(running),
                "queue_pending": len(pending),
                "queue_depth": len(running) + len(pending),
                "queue_prompt_ids": sorted(_queue_prompt_ids(queue)),
            })
            valid_fields.extend(("backend.queue_running", "backend.queue_pending", "backend.queue_depth"))
        except Exception as exc:
            collector_errors.append(f"backend queue: {exc}")
        try:
            stats = self.direct._json_request("/system_stats", timeout=3)
            system = stats.get("system") if isinstance(stats, dict) and isinstance(stats.get("system"), dict) else None
            devices = stats.get("devices") if isinstance(stats, dict) and isinstance(stats.get("devices"), list) else None
            backend["system"] = system
            backend["devices"] = devices
            if system is not None:
                valid_fields.append("backend.system")
            else:
                collector_errors.append("backend system_stats.system is missing")
            if devices:
                valid_fields.append("backend.devices")
            else:
                collector_errors.append("backend system_stats.devices is missing or empty")
            ram_total = system.get("ram_total") if isinstance(system, dict) else None
            ram_free = system.get("ram_free") if isinstance(system, dict) else None
            if isinstance(ram_total, (int, float)) and int(ram_total) > 0:
                backend["ram_total_bytes"] = int(ram_total)
                valid_fields.append("backend.ram_total_bytes")
            else:
                collector_errors.append("backend system_stats.system.ram_total is invalid")
            if isinstance(ram_free, (int, float)) and int(ram_free) >= 0:
                backend["ram_free_bytes"] = int(ram_free)
                valid_fields.append("backend.ram_free_bytes")
            else:
                collector_errors.append("backend system_stats.system.ram_free is invalid")
            vram_totals = [
                int(row.get("vram_total"))
                for row in (devices or [])
                if isinstance(row, dict) and isinstance(row.get("vram_total"), (int, float)) and int(row["vram_total"]) > 0
            ]
            vram_free = [
                int(row.get("vram_free"))
                for row in (devices or [])
                if isinstance(row, dict) and isinstance(row.get("vram_free"), (int, float)) and int(row["vram_free"]) >= 0
            ]
            if devices and len(vram_totals) == len(devices):
                backend["device_vram_total_bytes"] = vram_totals
                valid_fields.append("backend.device_vram_total_bytes")
            else:
                collector_errors.append("backend device vram_total telemetry is incomplete")
            if devices and len(vram_free) == len(devices):
                backend["device_vram_free_bytes"] = vram_free
                valid_fields.append("backend.device_vram_free_bytes")
            else:
                collector_errors.append("backend device vram_free telemetry is incomplete")
        except Exception as exc:
            collector_errors.append(f"backend system stats: {exc}")
        try:
            nvidia_smi = shutil.which("nvidia-smi")
            if not nvidia_smi:
                raise ProbeFailure("nvidia-smi is unavailable")
            completed = subprocess.run(
                [
                    nvidia_smi,
                    "--query-gpu=utilization.gpu,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if completed.returncode != 0:
                raise ProbeFailure(completed.stderr.strip() or f"exit {completed.returncode}")
            gpu_rows = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            utilization: list[int] = []
            temperatures: list[int] = []
            for row in gpu_rows:
                fields = [field.strip() for field in row.split(",")]
                if len(fields) != 2:
                    raise ProbeFailure(f"invalid nvidia-smi row: {row!r}")
                utilization.append(int(fields[0]))
                temperatures.append(int(fields[1]))
            if not utilization or len(utilization) != len(temperatures):
                raise ProbeFailure("nvidia-smi returned no complete GPU rows")
            backend["gpu_utilization_percent"] = utilization
            backend["gpu_temperature_c"] = temperatures
            valid_fields.extend(("backend.gpu_utilization_percent", "backend.gpu_temperature_c"))
        except Exception as exc:
            collector_errors.append(f"backend GPU utilization/temperature: {exc}")
        try:
            backend_pid = int(self.backend_scope["backend_pid"])
            start_ticks = _pid_start_ticks(backend_pid)
            process_cgroup = _pid_cgroup_path(backend_pid)
            inside = _cgroup_within(process_cgroup, str(self.backend_scope["campaign_cgroup_path"]))
            if start_ticks != int(self.backend_scope["backend_start_ticks"]):
                raise ProbeFailure("ComfyUI backend PID identity changed")
            matching_socket_inodes = sorted(
                _listening_socket_inodes(int(self.backend_scope["backend_port"]))
                & _pid_socket_inodes(backend_pid)
            )
            listening_socket_verified = bool(matching_socket_inodes)
            process_tree = _process_tree_metrics(backend_pid)
            backend.update({
                "process_pid": backend_pid,
                "process_start_ticks": start_ticks,
                "process_cgroup_path": process_cgroup,
                "process_inside_campaign_scope": inside,
                "process_listening_socket_verified": listening_socket_verified,
                "matching_listening_socket_inodes": matching_socket_inodes,
                "process_tree_pids": process_tree["pids"],
                "process_tree_rss_bytes": process_tree["rss_bytes"],
                "process_tree_threads": process_tree["threads"],
                "process_tree_fd_count": process_tree["fd_count"],
            })
            valid_fields.extend((
                "backend.process_pid",
                "backend.process_cgroup_path",
                "backend.process_inside_campaign_scope",
                "backend.process_listening_socket_verified",
                "backend.process_tree_pids",
                "backend.process_tree_rss_bytes",
                "backend.process_tree_threads",
                "backend.process_tree_fd_count",
            ))
            if not inside:
                collector_errors.append("ComfyUI backend process escaped campaign cgroup")
            if not listening_socket_verified:
                collector_errors.append("ComfyUI backend process no longer owns the configured listening socket")
        except Exception as exc:
            collector_errors.append(f"backend process identity/cgroup: {exc}")
        try:
            host["loadavg"] = Path("/proc/loadavg").read_text(encoding="utf-8").strip()
            valid_fields.append("host.loadavg")
        except Exception as exc:
            collector_errors.append(f"host loadavg: {exc}")
        try:
            meminfo = _read_meminfo()
            host["mem_available_bytes"] = int(meminfo["MemAvailable"])
            host["swap_free_bytes"] = int(meminfo["SwapFree"])
            valid_fields.extend(("host.mem_available_bytes", "host.swap_free_bytes"))
        except Exception as exc:
            collector_errors.append(f"host meminfo: {exc}")
        try:
            host["disk_free_bytes"] = int(shutil.disk_usage(self.artifact_root).free)
            valid_fields.append("host.disk_free_bytes")
        except Exception as exc:
            collector_errors.append(f"host disk: {exc}")
        for kind in ("cpu", "memory", "io"):
            try:
                host[f"psi_{kind}"] = _read_psi(kind)
                valid_fields.append(f"host.psi_{kind}")
            except Exception as exc:
                collector_errors.append(f"host {kind} PSI: {exc}")
        try:
            cgroup_path = _normalise_cgroup_path(str(self.backend_scope["campaign_cgroup_path"]))
            cgroup_root = (Path("/sys/fs/cgroup") / cgroup_path.lstrip("/")).resolve(strict=True)
            cgroup.update({
                "path": cgroup_path,
                "memory_high": _read_limit_value(cgroup_root / "memory.high"),
                "memory_current": _read_limit_value(cgroup_root / "memory.current"),
                "memory_max": _read_limit_value(cgroup_root / "memory.max"),
                "memory_swap_current": _read_limit_value(cgroup_root / "memory.swap.current"),
                "memory_swap_max": _read_limit_value(cgroup_root / "memory.swap.max"),
                "memory_events": _parse_key_value_file(cgroup_root / "memory.events"),
                "cpu_stat": _parse_key_value_file(cgroup_root / "cpu.stat"),
                "cpu_max": _read_cpu_max(cgroup_root / "cpu.max"),
                "pids_current": _read_limit_value(cgroup_root / "pids.current"),
                "pids_max": _read_limit_value(cgroup_root / "pids.max"),
            })
            valid_fields.extend({
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
            })
        except Exception as exc:
            collector_errors.append(f"cgroup v2: {exc}")

        hard_stop_reasons: list[str] = []
        if isinstance(host.get("mem_available_bytes"), int) and host["mem_available_bytes"] < self.min_mem_available_bytes:
            hard_stop_reasons.append("host_mem_available_below_limit")
        if isinstance(host.get("disk_free_bytes"), int) and host["disk_free_bytes"] < self.min_disk_free_bytes:
            hard_stop_reasons.append("artifact_disk_free_below_limit")
        if isinstance(backend.get("ram_free_bytes"), int) and backend["ram_free_bytes"] < self.min_mem_available_bytes:
            hard_stop_reasons.append("backend_ram_free_below_limit")
        if (
            isinstance(backend.get("process_tree_rss_bytes"), int)
            and backend["process_tree_rss_bytes"] >= EXPECTED_CGROUP_LIMITS["memory_high"]
        ):
            hard_stop_reasons.append("backend_process_tree_rss_at_or_above_memory_high")
        vram_free_values = backend.get("device_vram_free_bytes") if isinstance(backend.get("device_vram_free_bytes"), list) else []
        if vram_free_values and min(vram_free_values) < MIN_BACKEND_VRAM_FREE_BYTES:
            hard_stop_reasons.append("backend_vram_free_below_limit")
        gpu_temperatures = backend.get("gpu_temperature_c") if isinstance(backend.get("gpu_temperature_c"), list) else []
        if gpu_temperatures and max(gpu_temperatures) > MAX_GPU_TEMPERATURE_C:
            hard_stop_reasons.append("backend_gpu_temperature_above_limit")
        allowed_depth = self.max_queue_depth if allowed_queue_depth is None else int(allowed_queue_depth)
        if isinstance(backend.get("queue_depth"), int) and backend["queue_depth"] > allowed_depth:
            hard_stop_reasons.append("backend_queue_depth_above_limit")
        memory_events = cgroup.get("memory_events") if isinstance(cgroup.get("memory_events"), dict) else {}
        if self.baseline_memory_events is None and memory_events:
            self.baseline_memory_events = dict(memory_events)
        if self.baseline_memory_events:
            for key in ("oom", "oom_kill"):
                if int(memory_events.get(key) or 0) > int(self.baseline_memory_events.get(key) or 0):
                    hard_stop_reasons.append(f"cgroup_{key}_counter_increased")
        if cgroup.get("memory_high") != EXPECTED_CGROUP_LIMITS["memory_high"]:
            hard_stop_reasons.append("campaign_memory_high_mismatch")
        if cgroup.get("memory_max") != EXPECTED_CGROUP_LIMITS["memory_max"]:
            hard_stop_reasons.append("campaign_memory_max_mismatch")
        if cgroup.get("memory_swap_max") != EXPECTED_CGROUP_LIMITS["memory_swap_max"]:
            hard_stop_reasons.append("campaign_memory_swap_max_mismatch")
        cpu_max = cgroup.get("cpu_max") if isinstance(cgroup.get("cpu_max"), dict) else {}
        if (
            cpu_max.get("quota") != EXPECTED_CGROUP_LIMITS["cpu_quota"]
            or cpu_max.get("period") != EXPECTED_CGROUP_LIMITS["cpu_period"]
        ):
            hard_stop_reasons.append("campaign_cpu_max_mismatch")
        if cgroup.get("pids_max") != EXPECTED_CGROUP_LIMITS["pids_max"]:
            hard_stop_reasons.append("campaign_pids_max_mismatch")

        sample = {
            "sample_schema_version": SAFETY_SAMPLE_SCHEMA_VERSION,
            "sampled_at": utc_now(),
            "monotonic_seconds": round(time.monotonic(), 6),
            "phase": str(phase),
            "job_id": str(job_id or ""),
            "expected_fields": expected_fields,
            "valid_fields": sorted(set(valid_fields)),
            "missing_fields": sorted(set(expected_fields) - set(valid_fields)),
            "collector_errors": collector_errors,
            "backend": backend,
            "host": host,
            "cgroup": cgroup,
            "hard_limit_state": {
                "ok": not hard_stop_reasons,
                "reasons": hard_stop_reasons,
                "allowed_queue_depth": allowed_depth,
                "min_mem_available_bytes": self.min_mem_available_bytes,
                "min_disk_free_bytes": self.min_disk_free_bytes,
            },
        }
        self.sample_path.parent.mkdir(parents=True, exist_ok=True)
        with self.sample_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, ensure_ascii=False, default=str) + "\n")
        self.samples.append(sample)
        return sample

    def assert_preflight(self) -> dict[str, Any]:
        sample = self.sample("safe_profile_preflight", allowed_queue_depth=0)
        self.baseline_queue_ids = set(sample.get("backend", {}).get("queue_prompt_ids") or [])
        if sample["missing_fields"] or sample["collector_errors"]:
            raise ProbeFailure(
                "ComfyUI safety sample is incomplete: "
                f"missing={sample['missing_fields']}, errors={sample['collector_errors']}"
            )
        if sample["hard_limit_state"]["ok"] is not True:
            raise ProbeFailure(f"ComfyUI safety preflight hard-stop: {sample['hard_limit_state']}")
        if self.baseline_queue_ids:
            raise ProbeFailure(f"ComfyUI backend queue is not isolated at preflight: {sorted(self.baseline_queue_ids)}")
        return sample

    def abort_site_work(self, client: "WebClient", *, reason: str, job_id: str = "") -> dict[str, Any]:
        started = utc_now()
        try:
            status, payload = client.json(
                "POST",
                "/api/comfyui/interrupt",
                {"reason": str(reason)[:240]},
            )
        except Exception as exc:
            status = 0
            payload = {
                "ok": False,
                "error": f"site interrupt request failed: {exc}",
            }
        interrupt = payload.get("interrupt") if isinstance(payload.get("interrupt"), dict) else {}
        direct_fallback: dict[str, Any] | None = None
        if status != 200 or payload.get("ok") is not True or interrupt.get("backend_interrupted") is not True:
            try:
                raw = self.direct.interrupt(timeout_seconds=10)
                direct_fallback = {"ok": True, "result": raw}
            except Exception as exc:
                direct_fallback = {"ok": False, "error": str(exc)}

        queue_before_cleanup: dict[str, Any] = {}
        queue_after_cleanup: dict[str, Any] = {}
        delete_result: dict[str, Any] | None = None
        try:
            queue_before_cleanup = self.direct._json_request("/queue", timeout=10)
            new_prompt_ids = sorted(_queue_prompt_ids(queue_before_cleanup) - self.baseline_queue_ids)
            if new_prompt_ids:
                delete_result = {
                    "prompt_ids": new_prompt_ids,
                    "result": self.direct.delete_queue_items(new_prompt_ids, timeout_seconds=10),
                }
            queue_after_cleanup = self.direct._json_request("/queue", timeout=10)
        except Exception as exc:
            queue_after_cleanup = {"collector_error": str(exc)}

        terminal_job: dict[str, Any] = {}
        if job_id:
            deadline = time.monotonic() + max(5, self.cancel_grace_seconds)
            while time.monotonic() < deadline:
                try:
                    poll_status, poll_payload = client.json(
                        "GET",
                        f"/api/comfyui/jobs/{urllib.parse.quote(str(job_id))}",
                    )
                except Exception as exc:
                    terminal_job = {
                        "status": "poll_error",
                        "error": str(exc),
                    }
                    time.sleep(0.5)
                    continue
                if poll_status == 200 and poll_payload.get("ok") is True:
                    terminal_job = poll_payload.get("job") if isinstance(poll_payload.get("job"), dict) else {}
                    if str(terminal_job.get("status") or "").lower() in TERMINAL_JOB_STATES:
                        break
                time.sleep(0.5)
        try:
            final_sample = self.sample("bounded_abort_complete", job_id=job_id, allowed_queue_depth=0)
        except Exception as exc:
            final_sample = {
                "backend": {},
                "collector_errors": [f"bounded abort final sample failed: {exc}"],
                "hard_limit_state": {"ok": False},
            }
        terminal_ok = not job_id or str(terminal_job.get("status") or "").lower() in TERMINAL_JOB_STATES
        final_queue_depth = final_sample.get("backend", {}).get("queue_depth")
        queue_empty = isinstance(final_queue_depth, int) and final_queue_depth == 0
        final_hard_limit_ok = final_sample.get("hard_limit_state", {}).get("ok") is True
        interrupt_ok = bool(
            status == 200
            and payload.get("ok") is True
            and interrupt.get("backend_interrupted") is True
        ) or bool(direct_fallback and direct_fallback.get("ok") is True)
        receipt = {
            "reason": reason,
            "started_at": started,
            "finished_at": utc_now(),
            "job_id": job_id,
            "site_interrupt_http_status": status,
            "site_interrupt": payload,
            "direct_interrupt_fallback": direct_fallback,
            "queue_before_cleanup_ids": sorted(_queue_prompt_ids(queue_before_cleanup)),
            "queue_delete": delete_result,
            "queue_after_cleanup_ids": sorted(_queue_prompt_ids(queue_after_cleanup)),
            "terminal_job": terminal_job,
            "final_sample_index": len(self.samples) - 1,
            "interrupt_verified": interrupt_ok,
            "terminal_verified": terminal_ok,
            "queue_empty_verified": queue_empty,
            "final_hard_limit_ok": final_hard_limit_ok,
            "ok": bool(interrupt_ok and terminal_ok and queue_empty and final_hard_limit_ok),
        }
        self.abort_events.append(receipt)
        return receipt

    def summary(self) -> dict[str, Any]:
        expected = len(SAFETY_EXPECTED_FIELDS)
        total_slots = expected * len(self.samples)
        valid_slots = sum(len(set(item.get("valid_fields") or [])) for item in self.samples)
        completeness = (valid_slots / total_slots) if total_slots else 0.0
        monotonic_values = [
            float(item.get("monotonic_seconds"))
            for item in self.samples
            if isinstance(item.get("monotonic_seconds"), (int, float))
        ]
        sample_gaps = [
            later - earlier
            for earlier, later in zip(monotonic_values, monotonic_values[1:])
        ]
        return {
            "sample_schema_version": SAFETY_SAMPLE_SCHEMA_VERSION,
            "sample_path": str(self.sample_path),
            "limits": {
                "min_mem_available_bytes": self.min_mem_available_bytes,
                "min_disk_free_bytes": self.min_disk_free_bytes,
                "max_queue_depth": self.max_queue_depth,
                "cancel_grace_seconds": self.cancel_grace_seconds,
                "min_backend_vram_free_bytes": MIN_BACKEND_VRAM_FREE_BYTES,
                "max_gpu_temperature_c": MAX_GPU_TEMPERATURE_C,
                "expected_cgroup_limits": dict(EXPECTED_CGROUP_LIMITS),
            },
            "backend_scope": dict(self.backend_scope),
            "sample_count": len(self.samples),
            "expected_field_count": expected,
            "valid_field_slots": valid_slots,
            "total_field_slots": total_slots,
            "field_completeness_ratio": round(completeness, 6),
            "max_sample_gap_seconds": round(max(sample_gaps), 6) if sample_gaps else 0.0,
            "sample_gap_within_30_seconds": bool(self.samples and (not sample_gaps or max(sample_gaps) <= 30.0)),
            "samples_complete": bool(self.samples and all(not item.get("missing_fields") for item in self.samples)),
            "collector_errors": [
                error
                for item in self.samples
                for error in item.get("collector_errors") or []
            ],
            "hard_stop_samples": [
                index
                for index, item in enumerate(self.samples)
                if item.get("hard_limit_state", {}).get("ok") is not True
            ],
            "abort_events": self.abort_events,
        }


def _safe_abort_site_work(
    safety_monitor: ComfySafetyMonitor | None,
    client: "WebClient" | None,
    *,
    reason: str,
    job_id: str = "",
) -> dict[str, Any] | None:
    if safety_monitor is None or client is None:
        return None
    try:
        return safety_monitor.abort_site_work(client, reason=reason, job_id=job_id)
    except Exception as exc:
        return {
            "ok": False,
            "reason": reason,
            "job_id": job_id,
            "error": f"bounded abort raised unexpectedly: {exc}",
        }


def _assert_cleanup_safety(
    safety_monitor: ComfySafetyMonitor | None,
    client: "WebClient" | None,
    *,
    phase: str,
) -> dict[str, Any] | None:
    if safety_monitor is None:
        return None
    try:
        sample = safety_monitor.sample(phase, allowed_queue_depth=0)
    except Exception as exc:
        abort = _safe_abort_site_work(
            safety_monitor,
            client,
            reason=f"{phase}_collector_exception",
        )
        raise ProbeFailure(
            f"{phase} safety collector raised {exc.__class__.__name__}: {exc}; abort={abort}"
        ) from exc
    if (
        sample.get("missing_fields")
        or sample.get("collector_errors")
        or sample.get("hard_limit_state", {}).get("ok") is not True
    ):
        abort = _safe_abort_site_work(
            safety_monitor,
            client,
            reason=f"{phase}_resource_or_collector_hard_stop",
        )
        raise ProbeFailure(f"{phase} safety hard-stop: sample={sample}, abort={abort}")
    return sample


def require_campaign_comfyui_url(environ: dict[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    raw = str(env.get("HACKME_CAMPAIGN_COMFYUI_API_URL") or "").strip().rstrip("/")
    if not raw:
        raise ProbeFailure("HACKME_CAMPAIGN_COMFYUI_API_URL is required")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProbeFailure("HACKME_CAMPAIGN_COMFYUI_API_URL must be an http(s) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProbeFailure("ComfyUI campaign URL cannot contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ProbeFailure("ComfyUI campaign URL must not contain a path")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProbeFailure("ComfyUI campaign URL has an invalid port") from exc
    if port is not None and not 1 <= int(port) <= 65535:
        raise ProbeFailure("ComfyUI campaign URL port is outside 1-65535")
    return raw


def _read_response_body(response) -> bytes:
    try:
        return response.read()
    except http.client.IncompleteRead as exc:
        return exc.partial or b""


class WebClient:
    def __init__(self, base_url: str, *, insecure: bool = False, request_timeout_seconds: int = 15):
        self.base_url = str(base_url).rstrip("/")
        self.request_timeout_seconds = max(1, min(30, int(request_timeout_seconds)))
        self.jar = http.cookiejar.CookieJar()
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(self.jar)]
        if self.base_url.startswith("https://"):
            context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [("User-Agent", "hackme-formal-comfyui-probe/1.0")]
        self.csrf_token = ""

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        request = urllib.request.Request(
            self._url(path),
            data=body,
            method=method.upper(),
            headers=headers or {},
        )
        try:
            with self.opener.open(request, timeout=self.request_timeout_seconds) as response:
                return int(response.status), _read_response_body(response), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return int(exc.code), _read_response_body(exc), dict(exc.headers)

    @staticmethod
    def _decode_json(path: str, status: int, raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except (TypeError, ValueError) as exc:
            raise ProbeFailure(f"{path} returned non-JSON data (HTTP {status})") from exc
        if not isinstance(payload, dict):
            raise ProbeFailure(f"{path} returned a non-object JSON value (HTTP {status})")
        return payload

    def refresh_csrf(self) -> str:
        status, raw, _ = self.request("GET", "/api/csrf-token")
        payload = self._decode_json("/api/csrf-token", status, raw)
        token = str(payload.get("csrf_token") or "").strip()
        if status != 200 or not token:
            raise ProbeFailure(f"CSRF token acquisition failed (HTTP {status})")
        self.csrf_token = token
        return token

    def login(self, username: str, password: str) -> None:
        self.refresh_csrf()
        status, payload = self.json(
            "POST",
            "/api/login",
            {"username": username, "password": password},
            refresh_csrf=False,
        )
        if status != 200 or payload.get("ok") is not True:
            raise ProbeFailure(f"root login failed (HTTP {status}): {payload.get('msg') or 'unknown'}")
        self.refresh_csrf()

    def json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        refresh_csrf: bool | None = None,
    ) -> tuple[int, dict[str, Any]]:
        method = method.upper()
        if refresh_csrf is None:
            refresh_csrf = method not in {"GET", "HEAD"}
        if refresh_csrf:
            self.refresh_csrf()
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        status, raw, _ = self.request(method, path, body=body, headers=headers)
        return status, self._decode_json(path, status, raw)


def _expect_json(
    client: WebClient,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    statuses: set[int] | frozenset[int] = frozenset({200}),
    ok: bool | None = True,
) -> dict[str, Any]:
    status, body = client.json(method, path, payload)
    if status not in statuses:
        raise ProbeFailure(f"{method} {path} returned HTTP {status}, expected {sorted(statuses)}: {body}")
    if ok is not None and body.get("ok") is not ok:
        raise ProbeFailure(f"{method} {path} returned ok={body.get('ok')!r}, expected {ok}: {body}")
    body = dict(body)
    body["_http_status"] = status
    return body


def select_safe_gguf_profile(
    client: WebClient,
    *,
    expected_comfyui_url: str,
    max_size_bytes: int,
    models_root: Path,
) -> dict[str, Any]:
    if int(max_size_bytes) > SAFE_MODEL_MAX_FILE_BYTES:
        raise ProbeFailure("safe GGUF max bytes may only tighten the immutable 2 GiB cap")
    payload = _expect_json(client, "GET", "/api/comfyui/installed-gguf")
    actual_url = str(payload.get("comfyui_url") or "").strip().rstrip("/")
    if actual_url != str(expected_comfyui_url).strip().rstrip("/"):
        raise ProbeFailure(
            "installed GGUF inventory came from an unexpected backend: "
            f"expected={expected_comfyui_url}, actual={actual_url or '-'}"
        )
    installed = payload.get("installed_gguf_models") if isinstance(payload.get("installed_gguf_models"), list) else []
    profiles = payload.get("gguf_profiles") if isinstance(payload.get("gguf_profiles"), list) else []
    candidates: list[dict[str, Any]] = []
    rejection_reasons: list[dict[str, Any]] = []
    for allowlist_index, approved in enumerate(SAFE_GGUF_ALLOWLIST):
        profile = next(
            (
                row for row in profiles
                if isinstance(row, dict) and str(row.get("id") or "") == approved["profile_id"]
            ),
            None,
        )
        profile_variant = next(
            (
                row for row in (profile or {}).get("variants") or []
                if isinstance(row, dict) and str(row.get("id") or "") == approved["variant_id"]
            ),
            None,
        )
        inventory = next(
            (
                row for row in installed
                if isinstance(row, dict)
                and str(row.get("profile_id") or "") == approved["profile_id"]
                and str(row.get("variant_id") or "") == approved["variant_id"]
            ),
            None,
        )
        reasons: list[str] = []
        actual_files: dict[str, dict[str, Any]] = {}
        if not profile or not profile_variant:
            reasons.append("official_profile_or_variant_missing")
        else:
            if profile.get("enabled") is not True or profile_variant.get("enabled") is not True:
                reasons.append("official_profile_or_variant_disabled")
            if str(profile_variant.get("gguf_file") or "") != approved["gguf_file"]:
                reasons.append("official_variant_filename_drift")
            if int(profile_variant.get("size_bytes") or 0) != int(approved["size_bytes"]):
                reasons.append("official_variant_size_drift")
        if not inventory:
            reasons.append("approved_variant_not_installed")
        else:
            if inventory.get("official_profile") is not True or inventory.get("installed") is not True:
                reasons.append("inventory_not_official_or_installed")
            if inventory.get("enabled") is not True:
                reasons.append("inventory_variant_disabled")
            if str(inventory.get("gguf_file") or "") != approved["gguf_file"]:
                reasons.append("inventory_filename_mismatch")
            if int(inventory.get("size_bytes") or 0) != int(approved["size_bytes"]):
                reasons.append("inventory_size_mismatch")
            if int(inventory.get("size_bytes") or 0) > int(max_size_bytes):
                reasons.append("inventory_size_exceeds_safety_limit")
        approved_companions = approved.get("companions") if isinstance(approved.get("companions"), dict) else {}
        profile_companions = {
            str(row.get("slot") or ""): str(row.get("filename") or "")
            for row in (profile or {}).get("companions") or []
            if isinstance(row, dict) and str(row.get("slot") or "")
        }
        for slot in ("clip_name1", "clip_name2"):
            approved_row = approved_companions.get(slot) if isinstance(approved_companions.get(slot), dict) else {}
            if profile_companions.get(slot) != str(approved_row.get("filename") or ""):
                reasons.append(f"official_profile_{slot}_drift")
        exact_specs = {
            "gguf_file": {
                "class_type": "UnetLoaderGGUF",
                "input_name": "unet_name",
                "filename": approved["gguf_file"],
                "size_bytes": approved["size_bytes"],
            },
            **{
                slot: {
                    "class_type": "DualCLIPLoader" if slot.startswith("clip_") else "VAELoader",
                    "input_name": slot,
                    "filename": row.get("filename"),
                    "size_bytes": row.get("size_bytes"),
                }
                for slot, row in approved_companions.items()
                if isinstance(row, dict)
            },
        }
        for slot, spec in exact_specs.items():
            try:
                path = _resolve_model_file(
                    models_root,
                    str(spec["class_type"]),
                    str(spec["input_name"]),
                    str(spec["filename"]),
                )
                size = int(path.stat().st_size)
                if size != int(spec["size_bytes"]):
                    reasons.append(f"actual_{slot}_size_mismatch")
                    continue
                actual_files[slot] = {
                    "path": str(path),
                    "relative_path": path.relative_to(models_root).as_posix(),
                    "size_bytes": size,
                    "sha256": sha256_file(path),
                }
            except Exception as exc:
                reasons.append(f"actual_{slot}_stat_or_hash_failed:{exc}")
        actual_total_bytes = sum(int(row.get("size_bytes") or 0) for row in actual_files.values())
        if actual_total_bytes > SAFE_WORKFLOW_MODEL_TOTAL_BYTES:
            reasons.append("actual_safe_profile_model_total_exceeds_limit")
        if reasons:
            rejection_reasons.append({
                "profile_id": approved["profile_id"],
                "variant_id": approved["variant_id"],
                "reasons": reasons,
            })
            continue
        candidates.append({
            "profile_id": approved["profile_id"],
            "variant_id": approved["variant_id"],
            "gguf_file": approved["gguf_file"],
            "size_bytes": int(approved["size_bytes"]),
            "size_evidence": "versioned_allowlist_plus_actual_file_stat_and_sha256",
            "remote_file_stat_available": True,
            "max_size_bytes": int(max_size_bytes),
            "max_workflow_model_total_bytes": SAFE_WORKFLOW_MODEL_TOTAL_BYTES,
            "actual_model_total_bytes": actual_total_bytes,
            "actual_files": actual_files,
            "allowlist_index": allowlist_index,
            "official_profile_status": profile.get("status"),
            "official_variant_status": profile_variant.get("status"),
            "companion_models": {
                slot: str(row.get("filename") or "")
                for slot, row in approved_companions.items()
                if isinstance(row, dict) and str(row.get("filename") or "")
            },
            "profile_manifest_companion_models": profile_companions,
            "safe_vae_override": str((approved_companions.get("vae_name") or {}).get("filename") or ""),
            "inventory_name": inventory.get("name"),
            "backend_url": actual_url,
        })
        # The allowlist order is the deterministic policy.  Once the first
        # exact candidate has real stat+hash evidence, probing later variants
        # would only re-read several gigabytes without changing selection.
        break
    if not candidates:
        installed_summary = [
            {
                "profile_id": row.get("profile_id"),
                "variant_id": row.get("variant_id"),
                "gguf_file": row.get("gguf_file"),
                "size_bytes": row.get("size_bytes"),
                "enabled": row.get("enabled"),
            }
            for row in installed
            if isinstance(row, dict)
        ]
        raise ProbeFailure(
            "no installed ComfyUI GGUF model satisfies the explicit formal allowlist: "
            f"rejections={rejection_reasons}, installed={installed_summary}"
        )
    selected = dict(candidates[0])
    selected["selection_rule"] = "first_exact_match_in_versioned_allowlist"
    selected["allowlist"] = [dict(item) for item in SAFE_GGUF_ALLOWLIST]
    selected["rejections"] = rejection_reasons
    return selected


def select_safe_feature_checkpoint(
    client: WebClient,
    *,
    models_root: Path,
    requested_model: str,
) -> dict[str, Any]:
    requested_raw = str(requested_model or "").strip()
    requested = _normalise_model_name(requested_raw)
    if not requested:
        raise ProbeFailure(
            "HACKME_CAMPAIGN_COMFYUI_FEATURE_CHECKPOINT is required; generic feature coverage "
            "cannot substitute a GGUF diffusion model for a classic checkpoint"
        )
    payload = _expect_json(client, "GET", "/api/comfyui/models")
    models = payload.get("models")
    if not isinstance(models, list) or not all(isinstance(item, str) and item.strip() for item in models):
        raise ProbeFailure("ComfyUI checkpoint inventory has an invalid schema")
    exact_matches = [item for item in models if _normalise_model_name(item) == requested]
    if len(exact_matches) != 1:
        raise ProbeFailure(
            "safe feature checkpoint must be an exact, case-sensitive inventory entry: "
            f"requested={requested!r}, exact_match_count={len(exact_matches)}"
        )
    selected_inventory_value = exact_matches[0]
    path = _resolve_model_file(models_root, "CheckpointLoaderSimple", "ckpt_name", selected_inventory_value)
    size = int(path.stat().st_size)
    if size > SAFE_MODEL_MAX_FILE_BYTES:
        raise ProbeFailure(
            f"feature checkpoint exceeds immutable {SAFE_MODEL_MAX_FILE_BYTES}-byte safety cap: "
            f"{selected_inventory_value}={size}"
        )
    return {
        "ok": True,
        "selection_rule": "explicit_exact_inventory_match_actual_stat_sha256_no_fallback",
        "checkpoint": selected_inventory_value,
        "checkpoint_canonical": requested,
        "path": str(path),
        "relative_path": path.relative_to(models_root).as_posix(),
        "size_bytes": size,
        "sha256": sha256_file(path),
        "max_size_bytes": SAFE_MODEL_MAX_FILE_BYTES,
        "inventory_count": len(models),
    }


def select_safe_feature_dependencies(
    client: WebClient,
    *,
    models_root: Path,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    checkpoint = select_safe_feature_checkpoint(
        client,
        models_root=models_root,
        requested_model=str(env.get("HACKME_CAMPAIGN_COMFYUI_FEATURE_CHECKPOINT") or ""),
    )
    models_payload = _expect_json(client, "GET", "/api/comfyui/models")
    upscale_requested = str(env.get("HACKME_CAMPAIGN_COMFYUI_FEATURE_UPSCALE_MODEL") or "").strip()
    upscale_name = _normalise_model_name(upscale_requested)
    upscale_inventory = models_payload.get("upscale_models")
    if (
        not upscale_name
        or not isinstance(upscale_inventory, list)
        or any(not isinstance(item, str) or not item.strip() for item in upscale_inventory)
        or sum(1 for item in upscale_inventory if _normalise_model_name(item) == upscale_name) != 1
    ):
        raise ProbeFailure("safe feature upscaler must be one exact inventory entry")
    upscale_inventory_name = next(
        item for item in upscale_inventory if _normalise_model_name(item) == upscale_name
    )
    upscale_path = _resolve_model_file(models_root, "UpscaleModelLoader", "model_name", upscale_inventory_name)
    upscale_size = int(upscale_path.stat().st_size)
    if upscale_size > SAFE_MODEL_MAX_FILE_BYTES:
        raise ProbeFailure(f"feature upscaler exceeds immutable safety cap: {upscale_inventory_name}={upscale_size}")

    control_type = str(env.get("HACKME_CAMPAIGN_COMFYUI_FEATURE_CONTROLNET_TYPE") or "").strip().lower()
    control_requested = str(env.get("HACKME_CAMPAIGN_COMFYUI_FEATURE_CONTROLNET_MODEL") or "").strip()
    control_model = _normalise_model_name(control_requested)
    preprocessor = str(env.get("HACKME_CAMPAIGN_COMFYUI_FEATURE_CONTROLNET_PREPROCESSOR") or "").strip()
    type_map = models_payload.get("controlnet_types")
    type_info = type_map.get(control_type) if isinstance(type_map, dict) else None
    if not isinstance(type_info, dict) or type_info.get("available") is not True:
        raise ProbeFailure(f"safe feature ControlNet type is unavailable: {control_type or '-'}")
    matching_models = type_info.get("matching_models")
    preprocessors = type_info.get("available_preprocessors")
    if (
        not isinstance(matching_models, list)
        or any(not isinstance(item, str) or not item.strip() for item in matching_models)
        or sum(1 for item in matching_models if _normalise_model_name(item) == control_model) != 1
    ):
        raise ProbeFailure("safe feature ControlNet model must be one exact type-scoped inventory entry")
    if not isinstance(preprocessors, list) or sum(1 for item in preprocessors if item == preprocessor) != 1:
        raise ProbeFailure("safe feature ControlNet preprocessor must be one exact type-scoped inventory entry")
    control_inventory_name = next(
        item for item in matching_models if _normalise_model_name(item) == control_model
    )
    control_path = _resolve_model_file(models_root, "ControlNetLoader", "control_net_name", control_inventory_name)
    control_size = int(control_path.stat().st_size)
    if control_size > SAFE_MODEL_MAX_FILE_BYTES:
        raise ProbeFailure(f"feature ControlNet exceeds immutable safety cap: {control_inventory_name}={control_size}")
    total = int(checkpoint["size_bytes"]) + upscale_size + control_size
    if total > SAFE_WORKFLOW_MODEL_TOTAL_BYTES:
        raise ProbeFailure(f"feature dependency total exceeds immutable safety cap: {total}")
    return {
        "ok": True,
        "selection_rule": "all_explicit_exact_inventory_actual_stat_sha256_no_fallback",
        "checkpoint": checkpoint,
        "upscale_model": {
            "name": upscale_inventory_name,
            "canonical_name": upscale_name,
            "path": str(upscale_path),
            "relative_path": upscale_path.relative_to(models_root).as_posix(),
            "size_bytes": upscale_size,
            "sha256": sha256_file(upscale_path),
        },
        "controlnet": {
            "type": control_type,
            "model_name": control_inventory_name,
            "model_canonical_name": control_model,
            "preprocessor": preprocessor,
            "path": str(control_path),
            "relative_path": control_path.relative_to(models_root).as_posix(),
            "size_bytes": control_size,
            "sha256": sha256_file(control_path),
        },
        "actual_model_total_bytes": total,
        "max_file_bytes": SAFE_MODEL_MAX_FILE_BYTES,
        "max_total_bytes": SAFE_WORKFLOW_MODEL_TOTAL_BYTES,
    }


def wait_job(
    client: WebClient,
    job_id: str,
    *,
    timeout_seconds: int,
    safety_monitor: ComfySafetyMonitor | None = None,
    phase: str = "job",
) -> dict[str, Any]:
    deadline = time.monotonic() + max(5, int(timeout_seconds))
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            status, payload = client.json(
                "GET",
                f"/api/comfyui/jobs/{urllib.parse.quote(str(job_id))}",
            )
        except Exception as exc:
            abort = _safe_abort_site_work(
                safety_monitor,
                client,
                reason=f"{phase}_job_poll_exception",
                job_id=job_id,
            )
            raise ProbeFailure(
                f"job {job_id} poll raised {exc.__class__.__name__}: {exc}; abort={abort}"
            ) from exc
        if status != 200 or payload.get("ok") is not True:
            abort = _safe_abort_site_work(
                safety_monitor,
                client,
                reason=f"{phase}_job_poll_failed",
                job_id=job_id,
            )
            raise ProbeFailure(
                f"job {job_id} poll failed (HTTP {status}): {payload}; abort={abort}"
            )
        job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
        last = job
        state = str(job.get("status") or "").strip().lower()
        if safety_monitor is not None:
            try:
                sample = safety_monitor.sample(phase, job_id=job_id, allowed_queue_depth=1)
            except Exception as exc:
                abort = _safe_abort_site_work(
                    safety_monitor,
                    client,
                    reason=f"{phase}_safety_collector_exception",
                    job_id=job_id,
                )
                raise ProbeFailure(
                    f"job {job_id} safety collector raised {exc.__class__.__name__}: {exc}; "
                    f"abort={abort}"
                ) from exc
            unsafe = bool(
                sample.get("missing_fields")
                or sample.get("collector_errors")
                or sample.get("hard_limit_state", {}).get("ok") is not True
            )
            if unsafe:
                receipt = _safe_abort_site_work(
                    safety_monitor,
                    client,
                    reason=f"{phase}_resource_or_collector_hard_stop",
                    job_id=job_id,
                )
                raise ProbeFailure(
                    f"job {job_id} stopped by ComfyUI safety monitor: "
                    f"sample={sample}, abort={receipt}"
                )
        if state in TERMINAL_JOB_STATES:
            if safety_monitor is not None:
                queue_deadline = time.monotonic() + 10
                terminal_sample: dict[str, Any] = {}
                while time.monotonic() < queue_deadline:
                    try:
                        terminal_sample = safety_monitor.sample(
                            f"{phase}_terminal",
                            job_id=job_id,
                            allowed_queue_depth=0,
                        )
                    except Exception as exc:
                        abort = _safe_abort_site_work(
                            safety_monitor,
                            client,
                            reason=f"{phase}_terminal_collector_exception",
                            job_id=job_id,
                        )
                        raise ProbeFailure(
                            f"job {job_id} terminal safety collector raised "
                            f"{exc.__class__.__name__}: {exc}; abort={abort}"
                        ) from exc
                    terminal_queue_depth = terminal_sample.get("backend", {}).get("queue_depth")
                    if isinstance(terminal_queue_depth, int) and terminal_queue_depth == 0:
                        break
                    time.sleep(0.5)
                if (
                    terminal_sample.get("missing_fields")
                    or terminal_sample.get("collector_errors")
                    or terminal_sample.get("hard_limit_state", {}).get("ok") is not True
                ):
                    receipt = _safe_abort_site_work(
                        safety_monitor,
                        client,
                        reason=f"{phase}_terminal_queue_or_collector_hard_stop",
                        job_id=job_id,
                    )
                    raise ProbeFailure(
                        f"job {job_id} terminal cleanup was not safe: "
                        f"sample={terminal_sample}, abort={receipt}"
                    )
            return job
        time.sleep(1.0)
    abort = _safe_abort_site_work(
        safety_monitor,
        client,
        reason=f"{phase}_deadline_exceeded",
        job_id=job_id,
    )
    raise ProbeFailure(f"job {job_id} did not reach a terminal state: last={last}, abort={abort}")


def decode_data_url(data_url: str) -> tuple[str, bytes]:
    if not isinstance(data_url, str) or not data_url.startswith("data:") or "," not in data_url:
        raise ProbeFailure("output preview is not a data URL")
    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        raise ProbeFailure("output preview data URL is not base64 encoded")
    mime_type = header[5:].split(";", 1)[0].strip().lower()
    try:
        data = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ProbeFailure("output preview base64 is invalid") from exc
    if not mime_type or not data:
        raise ProbeFailure("output preview has an empty MIME type or body")
    return mime_type, data


def validate_image_bytes(data: bytes, *, expected_mime: str = "") -> dict[str, Any]:
    if len(data) < 64:
        raise ProbeFailure(f"image output is too small ({len(data)} bytes)")
    try:
        from PIL import Image, ImageStat

        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            detected = str(image.format or "").upper()
            sample = image.convert("RGB")
            sample.thumbnail((256, 256))
            stat = ImageStat.Stat(sample)
            stddev = sum(float(value) for value in stat.stddev) / max(1, len(stat.stddev))
    except Exception as exc:
        raise ProbeFailure(f"image output cannot be decoded: {exc}") from exc
    if width < 32 or height < 32:
        raise ProbeFailure(f"image output dimensions are implausible: {width}x{height}")
    if stddev < 1.0:
        raise ProbeFailure(f"image output is effectively blank (stddev={stddev:.3f})")
    mime = str(expected_mime or "").lower()
    allowed = {
        "PNG": {"", "image/png"},
        "JPEG": {"", "image/jpeg", "image/jpg"},
        "WEBP": {"", "image/webp"},
    }
    if detected not in allowed or mime not in allowed[detected]:
        raise ProbeFailure(f"image MIME/format mismatch: mime={mime or '-'}, format={detected or '-'}")
    return {
        "format": detected,
        "width": int(width),
        "height": int(height),
        "stddev": round(stddev, 4),
        "size_bytes": len(data),
        "sha256": sha256_bytes(data),
    }


def validate_media_file(path: Path, mime_type: str) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ProbeFailure(f"media artifact is missing or empty: {path}")
    mime = str(mime_type or "").lower()
    if mime.startswith("image/"):
        meta = validate_image_bytes(path.read_bytes(), expected_mime=mime)
        return {"kind": "image", **meta}
    if not (mime.startswith("video/") or mime.startswith("audio/")):
        raise ProbeFailure(f"unsupported official workflow output MIME type: {mime or '-'}")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ProbeFailure("ffprobe is required to validate audio/video ComfyUI outputs")
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=format_name,duration,size:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise ProbeFailure(f"ffprobe rejected {path}: {completed.stderr.strip()[:300]}")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise ProbeFailure(f"ffprobe returned invalid JSON for {path}") from exc
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    expected_kind = "video" if mime.startswith("video/") else "audio"
    if not any(str(item.get("codec_type") or "") == expected_kind for item in streams if isinstance(item, dict)):
        raise ProbeFailure(f"{path} contains no {expected_kind} stream")
    return {
        "kind": expected_kind,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "ffprobe": payload,
    }


def validate_feature_report(report: dict[str, Any]) -> dict[str, Any]:
    rows = report.get("results") if isinstance(report.get("results"), list) else []
    by_name = {
        str(item.get("name") or ""): item
        for item in rows
        if isinstance(item, dict) and str(item.get("name") or "")
    }
    missing = sorted(REQUIRED_FEATURE_ROWS - set(by_name))
    duplicates = sorted(
        name for name in REQUIRED_FEATURE_ROWS
        if sum(1 for item in rows if isinstance(item, dict) and item.get("name") == name) != 1
    )
    non_pass = {
        name: str(by_name.get(name, {}).get("status") or "missing")
        for name in sorted(REQUIRED_FEATURE_ROWS)
        if by_name.get(name, {}).get("ok") is not True or by_name.get(name, {}).get("status") != "pass"
    }
    ok = report.get("ok") is True and not missing and not duplicates and not non_pass
    return {
        "ok": ok,
        "required_rows": sorted(REQUIRED_FEATURE_ROWS),
        "missing": missing,
        "duplicates": duplicates,
        "non_pass": non_pass,
    }


def validate_workflow_input_cleanup(
    acceptance: dict[str, Any],
    job: dict[str, Any],
) -> dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    receipt = result.get("input_cleanup") if isinstance(result.get("input_cleanup"), dict) else {}
    accepted_run_id = str(acceptance.get("media_remap_run_id") or "").strip()
    terminal_run_id = str(receipt.get("run_id") or "").strip()
    try:
        accepted_count = int(acceptance.get("input_assignment_count"))
    except (TypeError, ValueError):
        accepted_count = -1
    try:
        terminal_count = int(result.get("input_assignment_count"))
    except (TypeError, ValueError):
        terminal_count = -1
    try:
        ref_count = int(receipt.get("input_ref_count"))
    except (TypeError, ValueError):
        ref_count = -1
    reasons: list[str] = []
    if receipt.get("schema_version") != 1:
        reasons.append("cleanup_schema_version_invalid")
    if receipt.get("ok") is not True or receipt.get("absence_verified") is not True:
        reasons.append("cleanup_not_exact_or_absence_unverified")
    if accepted_count < 0 or terminal_count != accepted_count:
        reasons.append("input_assignment_count_mismatch")
    if terminal_run_id != accepted_run_id:
        reasons.append("media_remap_run_id_mismatch")
    if accepted_count == 0:
        if accepted_run_id or terminal_run_id or ref_count != 0:
            reasons.append("no_temp_input_receipt_is_not_empty")
        if str(receipt.get("detail") or "") != "no_temp_inputs":
            reasons.append("no_temp_input_detail_missing")
    elif accepted_count > 0:
        cleanup = receipt.get("cleanup") if isinstance(receipt.get("cleanup"), dict) else {}
        refs = cleanup.get("refs") if isinstance(cleanup.get("refs"), list) else []
        method = str(cleanup.get("method") or "")
        binding = (
            cleanup.get("local_binding")
            if isinstance(cleanup.get("local_binding"), dict)
            else {}
        )
        if not accepted_run_id:
            reasons.append("media_remap_run_id_missing")
        if ref_count < accepted_count or len(refs) != ref_count:
            reasons.append("cleanup_ref_count_does_not_cover_assignments")
        if cleanup.get("ok") is not True or cleanup.get("absence_verified") is not True:
            reasons.append("nested_cleanup_not_exact_or_absence_unverified")
        if method == "local_filesystem":
            listener_pid = cleanup.get("listener_pid")
            listener_inode = str(cleanup.get("listener_inode") or "")
            listener_cwd = str(cleanup.get("listener_cwd") or "")
            project_dir = str(binding.get("project_dir") or "")
            listeners = binding.get("listeners") if isinstance(binding.get("listeners"), list) else []
            bound_listener = any(
                isinstance(item, dict)
                and item.get("pid") == listener_pid
                and str(item.get("inode") or "") == listener_inode
                and str(item.get("cwd") or "") == listener_cwd
                and item.get("cwd_matches_project") is True
                for item in listeners
            )
            if not (
                cleanup.get("binding_verified") is True
                and binding.get("binding_verified") is True
                and isinstance(listener_pid, int)
                and listener_pid > 0
                and listener_inode.isdigit()
                and listener_cwd
                and listener_cwd == project_dir
                and bound_listener
                and cleanup.get("directory_absent") is True
            ):
                reasons.append("local_backend_binding_or_directory_absence_invalid")
        elif method == "remote_delete_and_get":
            if not (
                cleanup.get("binding_verified") is False
                and binding.get("binding_verified") is False
                and cleanup.get("directory_absent") is None
            ):
                reasons.append("remote_cleanup_binding_evidence_invalid")
        else:
            reasons.append("cleanup_method_invalid")
        for index, row in enumerate(refs):
            if not isinstance(row, dict):
                reasons.append(f"cleanup_ref_{index}_malformed")
                continue
            ref = row.get("ref") if isinstance(row.get("ref"), dict) else {}
            if (
                str(ref.get("subfolder") or "") != accepted_run_id
                or str(ref.get("type") or "") != "input"
                or not str(ref.get("filename") or "")
                or row.get("absent") is not True
                or (
                    method == "local_filesystem"
                    and str(row.get("verification") or "") != "local_lstat"
                )
                or (
                    method == "remote_delete_and_get"
                    and str(row.get("verification") or "") != "http_404"
                )
            ):
                reasons.append(f"cleanup_ref_{index}_not_exact")
    return {
        "ok": not reasons,
        "accepted_run_id": accepted_run_id,
        "terminal_run_id": terminal_run_id,
        "accepted_assignment_count": accepted_count,
        "terminal_assignment_count": terminal_count,
        "input_ref_count": ref_count,
        "reasons": reasons,
        "receipt": receipt,
    }


def validate_final_model_safety_receipt(
    job: dict[str, Any],
    *,
    expected_backend_url: str = "",
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Independently validate the receipt bound to the exact queued graph."""

    env = os.environ if environ is None else environ
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    receipt = (
        result.get("final_model_safety")
        if isinstance(result.get("final_model_safety"), dict)
        else {}
    )
    errors: list[str] = []

    def _as_int(value: Any, default: int = -1) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default
    if receipt.get("schema_version") != "hackme.comfyui-final-model-safety/v1":
        errors.append("receipt_schema_version_invalid")
    if receipt.get("ok") is not True:
        errors.append("receipt_not_ok")
    if receipt.get("enforcement") != "campaign_final_graph_pre_prompt_fail_closed":
        errors.append("receipt_enforcement_invalid")

    def _valid_sha256(value: Any) -> bool:
        text = str(value or "")
        return len(text) == 64 and all(char in "0123456789abcdef" for char in text)

    graph_sha256 = str(receipt.get("graph_sha256") or "")
    receipt_sha256 = str(receipt.get("receipt_sha256") or "")
    if not _valid_sha256(graph_sha256):
        errors.append("graph_sha256_invalid")
    if not _valid_sha256(receipt_sha256):
        errors.append("receipt_sha256_invalid")
    if receipt:
        unsigned = dict(receipt)
        unsigned.pop("receipt_sha256", None)
        try:
            recomputed_receipt_sha256 = sha256_bytes(json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            recomputed_receipt_sha256 = ""
            errors.append(f"receipt_not_canonical_json:{exc}")
        if recomputed_receipt_sha256 != receipt_sha256:
            errors.append("receipt_sha256_mismatch")
    else:
        recomputed_receipt_sha256 = ""

    limits = receipt.get("limits") if isinstance(receipt.get("limits"), dict) else {}
    if limits != {
        "max_model_file_bytes": SAFE_MODEL_MAX_FILE_BYTES,
        "max_workflow_model_total_bytes": SAFE_WORKFLOW_MODEL_TOTAL_BYTES,
        "limits_can_only_tighten": True,
    }:
        errors.append("receipt_limits_not_immutable_exact")

    expected_root_text = str(env.get("HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT") or "").strip()
    try:
        expected_root = str(Path(expected_root_text).expanduser().resolve(strict=True))
    except Exception as exc:
        expected_root = ""
        errors.append(f"expected_models_root_unavailable:{exc}")
    if str(receipt.get("models_root_realpath") or "") != expected_root:
        errors.append("receipt_models_root_mismatch")

    terminal_backend = str(result.get("backend_url") or "").strip().rstrip("/")
    expected_backend = str(expected_backend_url or terminal_backend).strip().rstrip("/")
    if not expected_backend or terminal_backend != expected_backend:
        errors.append("terminal_backend_binding_mismatch")
    if str(receipt.get("backend_origin") or "").strip().rstrip("/") != expected_backend:
        errors.append("receipt_backend_binding_mismatch")

    files = receipt.get("model_files") if isinstance(receipt.get("model_files"), list) else []
    references = receipt.get("references") if isinstance(receipt.get("references"), list) else []
    if not files:
        errors.append("receipt_model_files_empty")
    relative_paths: list[str] = []
    total_bytes = 0
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            errors.append(f"model_file_{index}_malformed")
            continue
        relative_path = str(item.get("relative_path") or "")
        parts = relative_path.replace("\\", "/").split("/")
        if (
            not relative_path
            or relative_path.startswith(("/", "\\"))
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in parts)
        ):
            errors.append(f"model_file_{index}_relative_path_unsafe")
        relative_paths.append(relative_path)
        size_bytes = _as_int(item.get("size_bytes"))
        if size_bytes <= 0 or size_bytes > SAFE_MODEL_MAX_FILE_BYTES:
            errors.append(f"model_file_{index}_size_invalid")
        total_bytes += max(0, size_bytes)
        if not _valid_sha256(item.get("sha256")):
            errors.append(f"model_file_{index}_sha256_invalid")
        stat_receipt = item.get("stat") if isinstance(item.get("stat"), dict) else {}
        if (
            _as_int(stat_receipt.get("size_bytes")) != size_bytes
            or _as_int(stat_receipt.get("device")) < 0
            or _as_int(stat_receipt.get("inode")) <= 0
            or _as_int(stat_receipt.get("mode")) <= 0
            or _as_int(stat_receipt.get("link_count")) <= 0
            or _as_int(stat_receipt.get("mtime_ns")) <= 0
            or _as_int(stat_receipt.get("ctime_ns")) <= 0
        ):
            errors.append(f"model_file_{index}_stat_receipt_invalid")
    if len(set(relative_paths)) != len(relative_paths):
        errors.append("receipt_model_files_not_unique")
    if _as_int(receipt.get("distinct_model_file_count")) != len(files):
        errors.append("receipt_distinct_model_file_count_mismatch")
    if _as_int(receipt.get("distinct_model_total_bytes")) != total_bytes:
        errors.append("receipt_distinct_model_total_mismatch")
    if total_bytes > SAFE_WORKFLOW_MODEL_TOTAL_BYTES:
        errors.append("receipt_workflow_model_total_exceeds_limit")
    if _as_int(receipt.get("reference_count")) != len(references):
        errors.append("receipt_reference_count_mismatch")
    file_set = set(relative_paths)
    for index, reference in enumerate(references):
        if not isinstance(reference, dict):
            errors.append(f"reference_{index}_malformed")
            continue
        if str(reference.get("relative_path") or "") not in file_set:
            errors.append(f"reference_{index}_file_missing")
        if not all(str(reference.get(key) or "").strip() for key in (
            "node_id", "class_type", "input_name", "category", "kind", "name"
        )):
            errors.append(f"reference_{index}_identity_incomplete")

    filesystem_rows: list[dict[str, Any]] = []
    if expected_root:
        try:
            filesystem_rows = revalidate_final_model_safety_receipt_files(
                receipt,
                models_root=expected_root,
            )
        except (FinalModelSafetyError, OSError, TypeError, ValueError) as exc:
            errors.append(f"terminal_model_file_revalidation_failed:{exc}")
    if len(filesystem_rows) != len(files):
        errors.append("terminal_model_file_revalidated_count_mismatch")

    backend_binding = (
        result.get("final_model_safety_backend_binding")
        if isinstance(result.get("final_model_safety_backend_binding"), dict)
        else {}
    )
    if backend_binding.get("schema_version") != (
        "hackme.comfyui-final-model-safety-backend-binding/v1"
    ):
        errors.append("backend_history_binding_schema_invalid")
    if backend_binding.get("ok") is not True:
        errors.append("backend_history_binding_not_ok")
    if str(backend_binding.get("prompt_id") or "") != str(result.get("prompt_id") or ""):
        errors.append("backend_history_binding_prompt_id_mismatch")
    if str(backend_binding.get("graph_sha256") or "") != graph_sha256:
        errors.append("backend_history_binding_graph_sha256_mismatch")
    if str(backend_binding.get("receipt_sha256") or "") != receipt_sha256:
        errors.append("backend_history_binding_receipt_sha256_mismatch")
    if (
        backend_binding.get("history_graph_verified") is not True
        or backend_binding.get("history_marker_verified") is not True
        or _as_int(backend_binding.get("history_prompt_tuple_minimum_fields")) < 4
    ):
        errors.append("backend_history_binding_proof_incomplete")

    return {
        "ok": not errors,
        "schema_version": receipt.get("schema_version") or "",
        "graph_sha256": graph_sha256,
        "receipt_sha256": receipt_sha256,
        "recomputed_receipt_sha256": recomputed_receipt_sha256,
        "backend_origin": receipt.get("backend_origin") or "",
        "models_root_realpath": receipt.get("models_root_realpath") or "",
        "reference_count": len(references),
        "distinct_model_file_count": len(files),
        "distinct_model_total_bytes": total_bytes,
        "model_files": files,
        "terminal_model_file_revalidated_count": len(filesystem_rows),
        "terminal_model_files_unchanged": len(filesystem_rows) == len(files),
        "backend_history_binding": backend_binding,
        "backend_history_binding_verified": not any(
            error.startswith("backend_history_binding_") for error in errors
        ),
        "terminal_prompt_id": str(result.get("prompt_id") or ""),
        "errors": errors,
    }


def validate_official_report(report: dict[str, Any]) -> dict[str, Any]:
    expected = set(SYSTEM_WORKFLOW_IDS)
    results = report.get("results") if isinstance(report.get("results"), list) else []
    actual_ids = [str(item.get("bundle_id") or "") for item in results if isinstance(item, dict)]
    actual = set(actual_ids)
    duplicates = sorted({item for item in actual_ids if item and actual_ids.count(item) != 1})
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    bad_status: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            bad_status[f"row_{index}"] = {
                "status": "malformed",
                "issues": ["result row is not an object"],
                "error": "",
            }
            continue
        run_response = item.get("run_response") if isinstance(item.get("run_response"), dict) else {}
        run_json = run_response.get("json") if isinstance(run_response.get("json"), dict) else {}
        job = item.get("job") if isinstance(item.get("job"), dict) else {}
        cleanup_validation = validate_workflow_input_cleanup(run_json, job)
        if (
            item.get("status") != "passed"
            or bool(item.get("issues"))
            or bool(item.get("page_errors"))
            or cleanup_validation.get("ok") is not True
        ):
            bad_status[str(item.get("bundle_id") or f"row_{index}")] = {
                "status": item.get("status"),
                "issues": item.get("issues") or [],
                "error": item.get("error") or "",
                "stage": run_json.get("stage") or "",
                "gate": run_json.get("gate") or "",
                "dependency_status": run_json.get("dependency_status") or {},
                "bounded_abort": item.get("bounded_abort") or {},
                "input_cleanup_validation": cleanup_validation,
            }
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    error_console = [
        item for item in report.get("console_events") or []
        if isinstance(item, dict) and str(item.get("type") or "").lower() == "error"
    ]
    page_errors = list(report.get("page_errors") or [])
    network_errors = list(report.get("network_errors") or [])
    connection = report.get("connection") if isinstance(report.get("connection"), dict) else {}
    connection_body = connection.get("body") if isinstance(connection.get("body"), dict) else {}
    connection_ok = bool(
        int(connection.get("status") or 0) == 200
        and connection.get("ok") is True
        and connection_body.get("ok") is True
    )
    def summary_int(name: str) -> int:
        try:
            return int(summary[name])
        except (KeyError, TypeError, ValueError):
            return -1

    exact_counts = (
        summary_int("template_count") == len(expected)
        and summary_int("passed") == len(expected)
        and summary_int("failed") == 0
        and summary_int("completed_with_issues") == 0
    )
    return {
        "ok": bool(
            not missing
            and not unexpected
            and not duplicates
            and not bad_status
            and exact_counts
            and connection_ok
            and not error_console
            and not page_errors
            and not network_errors
        ),
        "expected_count": len(expected),
        "actual_count": len(results),
        "missing": missing,
        "unexpected": unexpected,
        "duplicates": duplicates,
        "bad_status": bad_status,
        "exact_counts": exact_counts,
        "connection_ok": connection_ok,
        "error_console_count": len(error_console),
        "page_error_count": len(page_errors),
        "network_error_count": len(network_errors),
    }


def _stop_child_process_group(process: subprocess.Popen, *, grace_seconds: int = 10) -> dict[str, Any]:
    process_group_id = int(process.pid)
    receipt: dict[str, Any] = {
        "pid": int(process.pid),
        "process_group_id": process_group_id,
        "term_sent": False,
        "kill_sent": False,
        "exit_code": process.poll(),
    }
    if process.poll() is not None:
        try:
            os.killpg(process_group_id, 0)
            receipt["group_gone"] = False
        except ProcessLookupError:
            receipt["group_gone"] = True
        if receipt["group_gone"] is True:
            return receipt
    try:
        os.killpg(process_group_id, signal.SIGTERM)
        receipt["term_sent"] = True
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=max(1, int(grace_seconds)))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
            receipt["kill_sent"] = True
        except ProcessLookupError:
            pass
        process.wait(timeout=10)
    receipt["exit_code"] = process.returncode
    try:
        os.killpg(process_group_id, 0)
        receipt["group_gone"] = False
    except ProcessLookupError:
        receipt["group_gone"] = True
    if receipt["group_gone"] is not True:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
            receipt["kill_sent"] = True
        except ProcessLookupError:
            pass
        time.sleep(0.2)
        try:
            os.killpg(process_group_id, 0)
            receipt["group_gone"] = False
        except ProcessLookupError:
            receipt["group_gone"] = True
    return receipt


def run_child(
    command: list[str],
    *,
    env: dict[str, str],
    log_path: Path,
    timeout_seconds: int,
    safety_monitor: ComfySafetyMonitor | None = None,
    site_client: WebClient | None = None,
    monitor_phase: str = "child",
) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + max(60, int(timeout_seconds))
    cleanup: dict[str, Any] = {}
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        while process.poll() is None:
            if time.monotonic() >= deadline:
                cleanup = _stop_child_process_group(process)
                abort = _safe_abort_site_work(
                    safety_monitor,
                    site_client,
                    reason=f"{monitor_phase}_child_deadline_exceeded",
                )
                raise ProbeFailure(
                    f"{monitor_phase} child exceeded {timeout_seconds}s: abort={abort}, cleanup={cleanup}"
                )
            if safety_monitor is not None:
                try:
                    sample = safety_monitor.sample(
                        monitor_phase,
                        allowed_queue_depth=safety_monitor.max_queue_depth,
                    )
                except Exception as exc:
                    cleanup = _stop_child_process_group(process)
                    abort = _safe_abort_site_work(
                        safety_monitor,
                        site_client,
                        reason=f"{monitor_phase}_collector_exception",
                    )
                    raise ProbeFailure(
                        f"{monitor_phase} safety collector raised {exc.__class__.__name__}: "
                        f"{exc}; abort={abort}, cleanup={cleanup}"
                    ) from exc
                if (
                    sample.get("missing_fields")
                    or sample.get("collector_errors")
                    or sample.get("hard_limit_state", {}).get("ok") is not True
                ):
                    cleanup = _stop_child_process_group(process)
                    abort = _safe_abort_site_work(
                        safety_monitor,
                        site_client,
                        reason=f"{monitor_phase}_resource_or_collector_hard_stop",
                    )
                    raise ProbeFailure(
                        f"{monitor_phase} child stopped by safety monitor: "
                        f"sample={sample}, abort={abort}, cleanup={cleanup}"
                    )
            time.sleep(2.0)
        exit_code = int(process.returncode or 0)
        cleanup = _stop_child_process_group(process, grace_seconds=3)
        if cleanup.get("group_gone") is not True:
            raise ProbeFailure(
                f"{monitor_phase} child process group remained after cleanup: {cleanup}"
            )
        if safety_monitor is not None:
            try:
                terminal_sample = safety_monitor.sample(
                    f"{monitor_phase}_terminal",
                    allowed_queue_depth=0,
                )
            except Exception as exc:
                abort = _safe_abort_site_work(
                    safety_monitor,
                    site_client,
                    reason=f"{monitor_phase}_terminal_collector_exception",
                )
                raise ProbeFailure(
                    f"{monitor_phase} terminal safety collector raised "
                    f"{exc.__class__.__name__}: {exc}; abort={abort}"
                ) from exc
            if (
                terminal_sample.get("missing_fields")
                or terminal_sample.get("collector_errors")
                or terminal_sample.get("hard_limit_state", {}).get("ok") is not True
            ):
                abort = _safe_abort_site_work(
                    safety_monitor,
                    site_client,
                    reason=f"{monitor_phase}_terminal_queue_or_collector_hard_stop",
                )
                raise ProbeFailure(
                    f"{monitor_phase} child left unsafe backend state: "
                    f"sample={terminal_sample}, abort={abort}"
                )
    return {
        "exit_code": exit_code,
        "duration_seconds": round(time.monotonic() - started, 3),
        "log": str(log_path),
        "log_sha256": sha256_file(log_path),
        "process_group_isolated": True,
        "cleanup": cleanup,
    }


def history_inventory(client: WebClient) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    payload = _expect_json(client, "GET", "/api/comfyui/history")
    rows = payload.get("history") if isinstance(payload.get("history"), list) else []
    inventory = {
        str(item.get("id")): item
        for item in rows
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    return inventory, payload


def workflow_inventory(client: WebClient) -> tuple[set[int], dict[str, Any]]:
    payload = _expect_json(client, "GET", "/api/comfyui/workflows")
    presets = payload.get("presets") if isinstance(payload.get("presets"), list) else []
    ids = {
        int(item.get("id"))
        for item in presets
        if isinstance(item, dict) and str(item.get("id") or "").isdigit()
    }
    return ids, payload


def settings_snapshot(client: WebClient) -> dict[str, Any]:
    settings = _expect_json(client, "GET", "/api/admin/settings").get("settings") or {}
    features = _expect_json(client, "GET", "/api/admin/features").get("features") or {}
    if not isinstance(settings, dict) or not isinstance(features, dict):
        raise ProbeFailure("admin settings/features snapshot is malformed")
    missing_settings = [key for key in SNAPSHOT_SETTING_KEYS if key not in settings]
    missing_features = [key for key in SNAPSHOT_FEATURE_KEYS if key not in features]
    if missing_settings or missing_features:
        raise ProbeFailure(
            f"settings snapshot is incomplete: settings={missing_settings}, features={missing_features}"
        )
    return {
        "settings": {key: settings[key] for key in SNAPSHOT_SETTING_KEYS},
        "features": {key: features[key] for key in SNAPSHOT_FEATURE_KEYS},
    }


def configure_formal_target(client: WebClient, *, comfyui_url: str) -> dict[str, Any]:
    feature_update = {
        "feature_comfyui_enabled": True,
        "feature_ai_agent_enabled": True,
        "feature_comfyui_template_importer_strict": True,
        "dangerous_confirm": list(SNAPSHOT_FEATURE_KEYS),
    }
    feature_payload = _expect_json(client, "PUT", "/api/admin/features", feature_update)
    settings_update = {
        "comfyui_connection_mode": "remote",
        "comfyui_remote_api_url": comfyui_url,
        "ai_agent_operation_mode": "write",
        "ai_agent_allowed_tools": "write_comfyui_generate",
        "module_ai_agent_min_role": "user",
        "dangerous_confirm": list(SNAPSHOT_SETTING_KEYS),
    }
    settings_payload = _expect_json(client, "PUT", "/api/admin/settings", settings_update)
    return {"features": feature_payload, "settings": settings_payload}


def restore_snapshot(client: WebClient, snapshot: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    settings = dict(snapshot.get("settings") or {})
    settings["dangerous_confirm"] = list(settings)
    status, settings_payload = client.json("PUT", "/api/admin/settings", settings)
    if status != 200 or settings_payload.get("ok") is not True:
        errors.append(f"settings restore failed HTTP {status}: {settings_payload}")
    features = dict(snapshot.get("features") or {})
    features["dangerous_confirm"] = list(features)
    status, feature_payload = client.json("PUT", "/api/admin/features", features)
    if status != 200 or feature_payload.get("ok") is not True:
        errors.append(f"feature restore failed HTTP {status}: {feature_payload}")
    try:
        after = settings_snapshot(client)
    except Exception as exc:
        after = {}
        errors.append(f"settings restore verification failed: {exc}")
    exact = after == snapshot
    if not exact:
        errors.append(f"settings/features differ after restore: expected={snapshot}, actual={after}")
    return {
        "ok": not errors and exact,
        "exact": exact,
        "errors": errors,
        "after": after,
        "settings_response": settings_payload,
        "features_response": feature_payload,
    }


def write_artifact(out_dir: Path, relative: str, data: bytes, *, mime_type: str) -> dict[str, Any]:
    path = out_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    validation = validate_image_bytes(data, expected_mime=mime_type) if mime_type.startswith("image/") else validate_media_file(path, mime_type)
    return {
        "path": str(path),
        "mime_type": mime_type,
        **validation,
    }


def validate_site_job_outputs(
    client: WebClient,
    job: dict[str, Any],
    *,
    out_dir: Path,
    label: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    state = str(job.get("status") or "").strip().lower()
    if state != "completed":
        raise ProbeFailure(f"{label} terminal state is {state or '-'}: {job.get('error') or ''}")
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    final_model_safety_validation = validate_final_model_safety_receipt(
        job,
        expected_backend_url=str(result.get("backend_url") or ""),
    )
    if final_model_safety_validation.get("ok") is not True:
        raise ProbeFailure(
            f"{label} final graph model safety receipt is invalid: "
            f"{final_model_safety_validation}"
        )
    job["_final_model_safety_validation"] = final_model_safety_validation
    images = result.get("images") if isinstance(result.get("images"), list) else []
    if not images and isinstance(result.get("image"), dict):
        images = [result["image"]]
    media = result.get("media") if isinstance(result.get("media"), list) else []
    if not images and not media:
        raise ProbeFailure(f"{label} completed without output references")
    artifacts: list[dict[str, Any]] = []
    image_records: list[dict[str, Any]] = []
    for index, item in enumerate(images, 1):
        if not isinstance(item, dict) or not isinstance(item.get("image_ref"), dict):
            raise ProbeFailure(f"{label} image #{index} has no image_ref")
        ref = item["image_ref"]
        preview = _expect_json(client, "POST", "/api/comfyui/image-preview", {"image_ref": ref})
        image = preview.get("image") if isinstance(preview.get("image"), dict) else {}
        mime, data = decode_data_url(str(image.get("data_url") or ""))
        if int(image.get("size_bytes") or -1) != len(data):
            raise ProbeFailure(f"{label} image #{index} preview size mismatch")
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".bin")
        artifact = write_artifact(out_dir, f"outputs/{label}_{index:02d}{suffix}", data, mime_type=mime)
        artifacts.append(artifact)
        image_records.append({"image_ref": ref, "prompt_id": result.get("prompt_id") or "", "artifact": artifact})
    for index, item in enumerate(media, 1):
        if not isinstance(item, dict) or not isinstance(item.get("file_ref"), dict):
            raise ProbeFailure(f"{label} media #{index} has no file_ref")
        preview = _expect_json(
            client,
            "POST",
            "/api/comfyui/media-preview",
            {"job_id": job.get("job_id"), "file_ref": item["file_ref"]},
        )
        output = preview.get("media") if isinstance(preview.get("media"), dict) else {}
        mime, data = decode_data_url(str(output.get("data_url") or ""))
        if int(output.get("size_bytes") or -1) != len(data):
            raise ProbeFailure(f"{label} media #{index} preview size mismatch")
        suffix = Path(str(item["file_ref"].get("filename") or "output.bin")).suffix or ".bin"
        artifacts.append(write_artifact(out_dir, f"outputs/{label}_media_{index:02d}{suffix}", data, mime_type=mime))
    return artifacts, image_records


def run_safe_gguf_canary(
    client: WebClient,
    *,
    selection: dict[str, Any],
    safety_monitor: ComfySafetyMonitor,
    out_dir: Path,
    run_id: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    workflows = _expect_json(client, "GET", "/api/comfyui/workflows")
    presets = workflows.get("presets") if isinstance(workflows.get("presets"), list) else []
    matches = [
        row for row in presets
        if isinstance(row, dict)
        and row.get("is_official") is True
        and str(row.get("system_bundle_id") or "") == SAFE_GGUF_WORKFLOW_ID
    ]
    if len(matches) != 1:
        raise ProbeFailure(
            f"safe GGUF workflow registry must contain exactly one official {SAFE_GGUF_WORKFLOW_ID}: "
            f"matches={len(matches)}"
        )
    preset_id = int(matches[0].get("id") or 0)
    if preset_id <= 0:
        raise ProbeFailure("safe GGUF workflow preset has no positive id")
    detail = _expect_json(client, "GET", f"/api/comfyui/workflows/{preset_id}")
    preset = detail.get("preset") if isinstance(detail.get("preset"), dict) else {}
    if str(preset.get("system_bundle_id") or "") != SAFE_GGUF_WORKFLOW_ID:
        raise ProbeFailure("safe GGUF workflow detail changed system_bundle_id")

    prompt = (
        f"formal safe GGUF canary {run_id}, adult research robot in a blue greenhouse, "
        "detailed lighting, non-blank composition"
    )
    negative = "blank, black, white, corrupt, low quality, text, watermark, child, minor"
    run_body = {
        "confirm_paid_api_nodes": True,
        "gguf_workflow": {
            "profile_id": selection["profile_id"],
            "variant_id": selection["variant_id"],
        },
        "vae": selection["safe_vae_override"],
        "user_inputs": {
            "3": {
                "steps": 2,
                "seed": 31415926,
                "cfg": 4.0,
                "sampler_name": "euler",
                "scheduler": "normal",
            },
            "5": {"width": 512, "height": 512, "batch_size": 1},
            "6": {"text": prompt},
            "7": {"text": negative},
        },
        "prompt": prompt,
        "negative_prompt": negative,
        "steps": 2,
        "width": 512,
        "height": 512,
        "seed": 31415926,
        "run_count": 1,
        "seed_after_generate": "fixed",
    }
    started = _expect_json(
        client,
        "POST",
        f"/api/comfyui/workflows/{preset_id}/run",
        run_body,
    )
    job_stub = started.get("job") if isinstance(started.get("job"), dict) else {}
    job_id = str(job_stub.get("job_id") or "")
    workflow_run_id = int(started.get("workflow_run_id") or 0)
    if not job_id or workflow_run_id <= 0:
        raise ProbeFailure(f"safe GGUF canary returned no job/workflow run id: {started}")
    job = wait_job(
        client,
        job_id,
        timeout_seconds=timeout_seconds,
        safety_monitor=safety_monitor,
        phase="safe_gguf_canary",
    )
    artifacts, image_records = validate_site_job_outputs(
        client,
        job,
        out_dir=out_dir,
        label="safe_gguf_canary",
    )
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    input_cleanup_validation = validate_workflow_input_cleanup(started, job)
    if input_cleanup_validation.get("ok") is not True:
        raise ProbeFailure(
            f"safe GGUF canary input cleanup was not exact: {input_cleanup_validation}"
        )
    terminal_run_id = int(result.get("workflow_run_id") or 0)
    if terminal_run_id != workflow_run_id:
        raise ProbeFailure(
            "safe GGUF canary workflow_run_id changed between acceptance and terminal result: "
            f"accepted={workflow_run_id}, terminal={terminal_run_id}"
        )
    if str(result.get("backend_url") or "").rstrip("/") != str(selection.get("backend_url") or "").rstrip("/"):
        raise ProbeFailure(
            "safe GGUF canary terminal result came from an unexpected backend: "
            f"{result.get('backend_url')!r}"
        )
    return ({
        "ok": True,
        "workflow_id": SAFE_GGUF_WORKFLOW_ID,
        "preset_id": preset_id,
        "workflow_run_id": workflow_run_id,
        "job_id": job_id,
        "terminal_status": job.get("status"),
        "strict_mode": started.get("strict_mode"),
        "profile_id": selection["profile_id"],
        "variant_id": selection["variant_id"],
        "gguf_file": selection["gguf_file"],
        "safe_vae_override": selection["safe_vae_override"],
        "size_bytes": selection["size_bytes"],
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "terminal_backend_url": result.get("backend_url") or "",
        "terminal_workflow_run_id": terminal_run_id,
        "media_remap_run_id": started.get("media_remap_run_id") or "",
        "input_assignment_count": result.get("input_assignment_count"),
        "input_cleanup": result.get("input_cleanup") or {},
        "input_cleanup_validation": input_cleanup_validation,
        "final_model_safety": result.get("final_model_safety") or {},
        "final_model_safety_validation": job.get("_final_model_safety_validation") or {},
    }, image_records)


def discard_images(
    client: WebClient,
    records: list[dict[str, Any]],
    *,
    safety_monitor: ComfySafetyMonitor | None = None,
) -> list[dict[str, Any]]:
    results = []
    seen: set[str] = set()
    for record in records:
        ref = record.get("image_ref") if isinstance(record.get("image_ref"), dict) else {}
        key = json.dumps(ref, ensure_ascii=False, sort_keys=True)
        if not ref or key in seen:
            continue
        seen.add(key)
        status, payload = client.json(
            "POST",
            "/api/comfyui/discard",
            {"image_ref": ref, "prompt_id": record.get("prompt_id") or ""},
        )
        discard = payload.get("discard") if isinstance(payload.get("discard"), dict) else {}
        binding = (
            discard.get("local_binding")
            if isinstance(discard.get("local_binding"), dict)
            else {}
        )
        verification = str(discard.get("verification") or "")
        if verification == "local_lstat_absent":
            listener_pid = binding.get("listener_pid")
            listener_inode = str(binding.get("listener_inode") or "")
            listener_cwd = str(binding.get("listener_cwd") or "")
            project_dir = str(binding.get("project_dir") or "")
            listeners = binding.get("listeners") if isinstance(binding.get("listeners"), list) else []
            proof_ok = bool(
                binding.get("binding_verified") is True
                and isinstance(listener_pid, int)
                and listener_pid > 0
                and listener_inode.isdigit()
                and listener_cwd
                and listener_cwd == project_dir
                and any(
                    isinstance(item, dict)
                    and item.get("pid") == listener_pid
                    and str(item.get("inode") or "") == listener_inode
                    and str(item.get("cwd") or "") == listener_cwd
                    and item.get("cwd_matches_project") is True
                    for item in listeners
                )
            )
        elif verification == "http_404":
            proof_ok = binding.get("binding_verified") is False
        else:
            proof_ok = False
        exact = bool(
            status == 200
            and payload.get("ok") is True
            and discard.get("absence_verified") is True
            and (
                discard.get("file_deleted") is True
                or discard.get("file_missing") is True
            )
            and discard.get("remote_preview_only") is False
            and proof_ok
        )
        results.append({
            "image_ref": ref,
            "http_status": status,
            "ok": exact,
            "warning": payload.get("warning") or "",
            "discard": discard,
        })
        if not exact:
            raise ProbeFailure(
                f"generated preview cleanup lacks exact absence proof: "
                f"HTTP {status}, payload={payload}"
            )
        _assert_cleanup_safety(
            safety_monitor,
            client,
            phase="discard_generated_preview_cleanup",
        )
    return results


def run_feature_probe(
    args: argparse.Namespace,
    *,
    client: WebClient,
    safety_monitor: ComfySafetyMonitor,
    comfyui_url: str,
    child_env: dict[str, str],
    out_dir: Path,
    feature_checkpoint: dict[str, Any],
) -> dict[str, Any]:
    if feature_checkpoint.get("ok") is not True:
        raise ProbeFailure(f"safe feature checkpoint selection is not valid: {feature_checkpoint}")
    report_path = out_dir / "feature_probe.json"
    child = run_child(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/comfyui/feature_probe.py"),
            "--base-url",
            args.base_url,
            "--timeout",
            str(args.feature_timeout),
            "--model",
            str(feature_checkpoint["checkpoint"]["checkpoint"]),
            "--upscale-model",
            str(feature_checkpoint["upscale_model"]["name"]),
            "--controlnet-type",
            str(feature_checkpoint["controlnet"]["type"]),
            "--controlnet-model",
            str(feature_checkpoint["controlnet"]["model_name"]),
            "--controlnet-preprocessor",
            str(feature_checkpoint["controlnet"]["preprocessor"]),
            "--probe-run-id",
            str(out_dir.name),
            "--cancel-grace",
            str(args.safety_cancel_grace_seconds),
            "--http-timeout",
            "10",
            "--json-out",
            str(report_path),
            *( ["--insecure"] if args.insecure else [] ),
        ],
        env=child_env,
        log_path=out_dir / "logs/feature_probe.log",
        timeout_seconds=(args.feature_timeout * 8) + 600,
        safety_monitor=safety_monitor,
        site_client=client,
        monitor_phase="feature_probe",
    )
    if not report_path.is_file():
        raise ProbeFailure(f"feature probe produced no JSON report: {child}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    strict = validate_feature_report(report)
    if child["exit_code"] != 0 or not strict["ok"]:
        raise ProbeFailure(f"strict feature probe failed: child={child}, validation={strict}")
    status = _expect_json(WebClient(args.base_url, insecure=args.insecure), "GET", "/api/version", ok=None)
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    created_history_ids = summary.get("created_history_ids") if isinstance(summary.get("created_history_ids"), list) else []
    if (
        not created_history_ids
        or any(not isinstance(item, int) or item <= 0 for item in created_history_ids)
        or len(set(created_history_ids)) != len(created_history_ids)
    ):
        raise ProbeFailure(f"feature probe history correlation inventory is invalid: {created_history_ids}")
    return {
        "ok": True,
        "child": child,
        "validation": strict,
        "report_path": str(report_path),
        "target": status,
        "comfyui_url": comfyui_url,
        "feature_checkpoint": feature_checkpoint,
        "created_history_ids": created_history_ids,
        "input_cleanup": summary.get("input_cleanup") or {},
    }


def run_mandatory_dependency_preflight(
    args: argparse.Namespace,
    *,
    comfyui_url: str,
    safe_selection: dict[str, Any],
    safety_monitor: ComfySafetyMonitor,
    client: WebClient,
    child_env: dict[str, str],
    out_dir: Path,
    models_root: Path,
    model_safety: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model_safety = (
        dict(model_safety)
        if isinstance(model_safety, dict)
        else audit_official_workflow_model_safety(models_root)
    )
    try:
        feature_checkpoint = select_safe_feature_dependencies(
            client,
            models_root=models_root,
        )
    except Exception as exc:
        feature_checkpoint = {"ok": False, "error": str(exc)}
    preflight_dir = out_dir / "dependency_preflight"
    all_report_path = preflight_dir / "all_official_defaults.json"
    safe_report_path = preflight_dir / "safe_gguf_override.json"
    common = [
        sys.executable,
        str(REPO_ROOT / "scripts/comfyui/official_workflow_probe.py"),
        "--comfyui-url",
        comfyui_url,
        "--request-timeout",
        "30",
        "--preflight-only",
    ]
    all_child = run_child(
        [
            *common,
            "--continue-on-fail",
            "--json-out",
            str(all_report_path),
        ],
        env=child_env,
        log_path=out_dir / "logs/dependency_preflight_all.log",
        timeout_seconds=600,
        safety_monitor=safety_monitor,
        site_client=client,
        monitor_phase="dependency_preflight_all_32",
    )
    if not all_report_path.is_file():
        raise ProbeFailure(f"mandatory dependency preflight produced no all-workflow report: {all_child}")
    all_report = json.loads(all_report_path.read_text(encoding="utf-8"))

    companions = safe_selection.get("companion_models") if isinstance(safe_selection.get("companion_models"), dict) else {}
    missing_companions = [key for key in ("clip_name1", "clip_name2", "vae_name") if not str(companions.get(key) or "")]
    if missing_companions:
        raise ProbeFailure(f"allowlisted GGUF profile has incomplete companion mapping: {missing_companions}")
    safe_node_inputs = {
        "4": {"unet_name": safe_selection["gguf_file"]},
        "10": {
            "clip_name1": companions["clip_name1"],
            "clip_name2": companions["clip_name2"],
        },
        "11": {"vae_name": companions["vae_name"]},
    }
    safe_child = run_child(
        [
            *common,
            "--only",
            SAFE_GGUF_WORKFLOW_ID,
            "--custom-params",
            "--custom-param-json",
            json.dumps({"node_inputs": safe_node_inputs}, ensure_ascii=False, separators=(",", ":")),
            "--json-out",
            str(safe_report_path),
        ],
        env=child_env,
        log_path=out_dir / "logs/dependency_preflight_safe_gguf.log",
        timeout_seconds=300,
        safety_monitor=safety_monitor,
        site_client=client,
        monitor_phase="dependency_preflight_safe_gguf",
    )
    if not safe_report_path.is_file():
        raise ProbeFailure(f"safe GGUF dependency preflight produced no report: {safe_child}")
    safe_report = json.loads(safe_report_path.read_text(encoding="utf-8"))

    all_rows = all_report.get("results") if isinstance(all_report.get("results"), list) else []
    safe_rows = safe_report.get("results") if isinstance(safe_report.get("results"), list) else []
    rows_by_id = {
        str(row.get("bundle_id") or ""): row
        for row in all_rows
        if isinstance(row, dict) and str(row.get("bundle_id") or "")
    }
    if len(safe_rows) == 1 and isinstance(safe_rows[0], dict):
        rows_by_id[SAFE_GGUF_WORKFLOW_ID] = safe_rows[0]
    expected = set(SYSTEM_WORKFLOW_IDS)
    actual = set(rows_by_id)
    missing_workflows = sorted(expected - actual)
    unexpected_workflows = sorted(actual - expected)
    dependency_failures: dict[str, Any] = {}
    source_dependency_contracts: dict[str, Any] = {}
    for bundle_id in sorted(expected):
        row = rows_by_id.get(bundle_id) if isinstance(rows_by_id.get(bundle_id), dict) else {}
        preflight = row.get("preflight") if isinstance(row.get("preflight"), dict) else {}
        source_contract = (
            preflight.get("dependency_contract")
            if isinstance(preflight.get("dependency_contract"), dict)
            else {}
        )
        source_dependency_contracts[bundle_id] = source_contract
        contract_ok = bool(
            preflight.get("source_dependency_contract_valid") is True
            and source_contract.get("schema_version")
            == "hackme.comfyui-manifest-dependency-contract/v1"
            and source_contract.get("ok") is True
            and not list(source_contract.get("errors") or [])
        )
        if (
            row.get("status") != "preflight_pass"
            or preflight.get("runnable") is not True
            or not contract_ok
        ):
            dependency_failures[bundle_id] = {
                "status": row.get("status") or "missing",
                "missing_nodes": preflight.get("missing_nodes") or [],
                "missing_models": preflight.get("missing_models") or [],
                "source_dependency_contract_valid": contract_ok,
                "source_dependency_contract_errors": source_contract.get("errors") or [],
                "source_dependency_contract_differences": source_contract.get("differences") or {},
                "detail": row.get("detail") or "",
            }
    source_dependency_contracts_ok = bool(
        set(source_dependency_contracts) == expected
        and all(
            isinstance(contract, dict)
            and contract.get("schema_version")
            == "hackme.comfyui-manifest-dependency-contract/v1"
            and contract.get("ok") is True
            and not list(contract.get("errors") or [])
            for contract in source_dependency_contracts.values()
        )
    )
    safe_override_ok = bool(
        safe_child.get("exit_code") == 0
        and len(safe_rows) == 1
        and safe_rows[0].get("bundle_id") == SAFE_GGUF_WORKFLOW_ID
        and safe_rows[0].get("status") == "preflight_pass"
        and (safe_rows[0].get("preflight") or {}).get("runnable") is True
        and (safe_rows[0].get("preflight") or {}).get("source_dependency_contract_valid") is True
    )
    return {
        "ok": bool(
            not missing_workflows
            and not unexpected_workflows
            and not dependency_failures
            and source_dependency_contracts_ok
            and safe_override_ok
            and model_safety.get("ok") is True
            and feature_checkpoint.get("ok") is True
        ),
        "expected_count": len(expected),
        "actual_count": len(rows_by_id),
        "missing_workflows": missing_workflows,
        "unexpected_workflows": unexpected_workflows,
        "dependency_failures": dependency_failures,
        "source_dependency_contract_count": len(source_dependency_contracts),
        "source_dependency_contracts_ok": source_dependency_contracts_ok,
        "source_dependency_contracts": source_dependency_contracts,
        "safe_override_ok": safe_override_ok,
        "safe_profile_id": safe_selection["profile_id"],
        "safe_variant_id": safe_selection["variant_id"],
        "model_safety": model_safety,
        "feature_checkpoint": feature_checkpoint,
        "all_child": all_child,
        "safe_child": safe_child,
        "all_report_path": str(all_report_path),
        "safe_report_path": str(safe_report_path),
    }


def run_official_templates(
    args: argparse.Namespace,
    *,
    client: WebClient,
    safety_monitor: ComfySafetyMonitor,
    safe_selection: dict[str, Any],
    child_env: dict[str, str],
    out_dir: Path,
) -> dict[str, Any]:
    qa_dir = out_dir / "official_templates"
    child = run_child(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/testing/playwright_comfyui_template_default_qa.py"),
            "--base-url",
            args.base_url,
            "--comfyui-api-url",
            require_campaign_comfyui_url(),
            "--out-dir",
            str(qa_dir),
            "--per-template-timeout",
            str(args.official_timeout),
            "--poll-seconds",
            "2",
            "--request-timeout",
            "30",
            "--gguf-profile",
            safe_selection["profile_id"],
            "--gguf-variant",
            safe_selection["variant_id"],
            "--gguf-vae",
            safe_selection["safe_vae_override"],
        ],
        env=child_env,
        log_path=out_dir / "logs/official_templates.log",
        timeout_seconds=(args.official_timeout * len(SYSTEM_WORKFLOW_IDS)) + 1200,
        safety_monitor=safety_monitor,
        site_client=client,
        monitor_phase="official_templates_all_32",
    )
    report_path = qa_dir / "results.json"
    if not report_path.is_file():
        partial = qa_dir / "results.partial.json"
        raise ProbeFailure(f"official template runner produced no final report; partial={partial.is_file()}, child={child}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    strict = validate_official_report(report)
    artifact_rows: list[dict[str, Any]] = []
    image_records: list[dict[str, Any]] = []
    input_cleanup_validations: list[dict[str, Any]] = []
    final_model_safety_validations: list[dict[str, Any]] = []
    for item in report.get("results") or []:
        if not isinstance(item, dict):
            continue
        for output in (item.get("images") or []) + (item.get("media") or []):
            if not isinstance(output, dict):
                continue
            path_text = str(output.get("path") or "")
            mime = str(output.get("mime_type") or "")
            if not path_text:
                raise ProbeFailure(f"official workflow {item.get('bundle_id')} has an output without an artifact path")
            path = Path(path_text).resolve()
            if qa_dir.resolve() not in path.parents:
                raise ProbeFailure(f"official output escaped artifact root: {path}")
            artifact_rows.append({"bundle_id": item.get("bundle_id"), "path": str(path), "mime_type": mime, **validate_media_file(path, mime)})
        screenshot = Path(str(item.get("preview_screenshot") or ""))
        if not screenshot.is_file():
            raise ProbeFailure(f"official workflow {item.get('bundle_id')} has no preview screenshot")
        validate_image_bytes(screenshot.read_bytes(), expected_mime="image/png")
        job = item.get("job") if isinstance(item.get("job"), dict) else {}
        run_response = item.get("run_response") if isinstance(item.get("run_response"), dict) else {}
        acceptance = run_response.get("json") if isinstance(run_response.get("json"), dict) else {}
        cleanup_validation = validate_workflow_input_cleanup(acceptance, job)
        input_cleanup_validations.append({
            "bundle_id": str(item.get("bundle_id") or ""),
            **cleanup_validation,
        })
        terminal_result = job.get("result") if isinstance(job.get("result"), dict) else {}
        final_model_safety_validation = validate_final_model_safety_receipt(
            job,
            expected_backend_url=str(terminal_result.get("backend_url") or ""),
        )
        final_model_safety_validations.append({
            "bundle_id": str(item.get("bundle_id") or ""),
            **final_model_safety_validation,
        })
        result = terminal_result
        for image in result.get("images") or []:
            if isinstance(image, dict) and isinstance(image.get("image_ref"), dict):
                image_records.append({
                    "image_ref": image["image_ref"],
                    "prompt_id": result.get("prompt_id") or "",
                })
    if child["exit_code"] != 0 or not strict["ok"]:
        raise ProbeFailure(f"official template execution failed: child={child}, validation={strict}")
    if (
        len(input_cleanup_validations) != len(SYSTEM_WORKFLOW_IDS)
        or any(row.get("ok") is not True for row in input_cleanup_validations)
    ):
        raise ProbeFailure(
            f"official template input cleanup was not exact: {input_cleanup_validations}"
        )
    if (
        len(final_model_safety_validations) != len(SYSTEM_WORKFLOW_IDS)
        or any(row.get("ok") is not True for row in final_model_safety_validations)
    ):
        raise ProbeFailure(
            "official template final graph model safety receipts were invalid: "
            f"{final_model_safety_validations}"
        )
    if len(artifact_rows) < len(SYSTEM_WORKFLOW_IDS):
        raise ProbeFailure(
            f"official templates yielded only {len(artifact_rows)} decoded outputs for {len(SYSTEM_WORKFLOW_IDS)} workflows"
        )
    return {
        "ok": True,
        "child": child,
        "validation": strict,
        "report_path": str(report_path),
        "artifact_count": len(artifact_rows),
        "artifacts": artifact_rows,
        "input_cleanup_validated_count": len(input_cleanup_validations),
        "input_cleanup_validations": input_cleanup_validations,
        "final_model_safety_validated_count": len(final_model_safety_validations),
        "final_model_safety_validations": final_model_safety_validations,
        "_image_records": image_records,
    }


def run_custom_workflow(
    client: WebClient,
    *,
    safe_selection: dict[str, Any],
    safety_monitor: ComfySafetyMonitor,
    out_dir: Path,
    run_id: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    workflows = _expect_json(client, "GET", "/api/comfyui/workflows")
    presets = workflows.get("presets") if isinstance(workflows.get("presets"), list) else []
    source = next(
        (
            row for row in presets
            if isinstance(row, dict)
            and row.get("is_official") is True
            and str(row.get("system_bundle_id") or "") == SAFE_GGUF_WORKFLOW_ID
        ),
        None,
    )
    source_id = int((source or {}).get("id") or 0)
    if source_id <= 0:
        raise ProbeFailure("custom safe workflow source preset is missing")
    source_detail = _expect_json(client, "GET", f"/api/comfyui/workflows/{source_id}")
    source_preset = source_detail.get("preset") if isinstance(source_detail.get("preset"), dict) else {}
    workflow = source_preset.get("workflow_json") if isinstance(source_preset.get("workflow_json"), dict) else {}
    if not workflow:
        raise ProbeFailure("custom safe workflow source has no workflow_json")
    prompt = f"formal custom safe GGUF workflow {run_id}, geometric lighthouse, detailed sky"
    negative = "blank, black, white, corrupt, text, watermark, child, minor"
    user_inputs = {
        "3": {
            "steps": 2,
            "cfg": 4.0,
            "seed": 24681358,
            "sampler_name": "euler",
            "scheduler": "normal",
        },
        "5": {"width": 512, "height": 512, "batch_size": 1},
        "6": {"text": prompt},
        "7": {"text": negative},
    }
    selected = apply_gguf_workflow_profile(
        workflow,
        user_inputs,
        {
            "profile_id": safe_selection["profile_id"],
            "variant_id": safe_selection["variant_id"],
        },
    )
    workflow = selected.workflow
    user_inputs = selected.user_inputs
    safe_vae = str(safe_selection.get("safe_vae_override") or "")
    vae_node = workflow.get("11") if isinstance(workflow.get("11"), dict) else {}
    vae_inputs = vae_node.get("inputs") if isinstance(vae_node.get("inputs"), dict) else {}
    if not safe_vae or not vae_node or "vae_name" not in vae_inputs:
        raise ProbeFailure("custom safe GGUF workflow cannot apply the attested VAE override")
    vae_inputs["vae_name"] = safe_vae
    user_inputs.setdefault("11", {})["vae_name"] = safe_vae
    workflow_sha256 = sha256_bytes(
        json.dumps(workflow, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    defaults = {
        "generation_mode": "txt2img",
        "model": safe_selection["gguf_file"],
        "diffusion_model": safe_selection["gguf_file"],
        "gguf_profile": safe_selection["profile_id"],
        "gguf_variant": safe_selection["variant_id"],
        "vae": safe_vae,
        "prompt": prompt,
        "negative_prompt": negative,
        "width": 512,
        "height": 512,
        "steps": 2,
        "cfg": 4.0,
        "seed": 24681358,
        "batch_size": 1,
        "sampler_name": "euler",
        "scheduler": "normal",
    }
    preset_id = 0
    deleted: dict[str, Any] = {}
    delete_status = 0
    primary_error: Exception | None = None
    result_payload: dict[str, Any] | None = None
    image_records: list[dict[str, Any]] = []
    try:
        imported = _expect_json(
            client,
            "POST",
            "/api/comfyui/workflows/import",
            {
                "title": f"Formal Custom Safe GGUF Workflow {run_id}",
                "description": "formal campaign temporary allowlisted GGUF workflow",
                "visibility": "private",
                "workflow_json": workflow,
                "default_params": defaults,
                "layout_json": source_preset.get("layout_json") or {},
            },
        )
        preset = imported.get("preset") if isinstance(imported.get("preset"), dict) else {}
        preset_id = int(preset.get("id") or 0)
        if preset_id <= 0:
            raise ProbeFailure("custom workflow import returned no preset id")
        updated_title = f"Formal Custom Safe GGUF Workflow Edited {run_id}"
        updated = _expect_json(
            client,
            "PUT",
            f"/api/comfyui/workflows/{preset_id}",
            {
                "title": updated_title,
                "description": "formal campaign temporary allowlisted GGUF workflow, edited",
                "visibility": "private",
                "workflow_json": workflow,
                "default_params": defaults,
                "layout_json": source_preset.get("layout_json") or {},
            },
        )
        if str((updated.get("preset") or {}).get("title") or "") != updated_title:
            raise ProbeFailure("custom workflow update did not persist the edited title")
        detail = _expect_json(client, "GET", f"/api/comfyui/workflows/{preset_id}")
        if str((detail.get("preset") or {}).get("title") or "") != updated_title:
            raise ProbeFailure("custom workflow detail does not reflect the edit")
        started = _expect_json(
            client,
            "POST",
            f"/api/comfyui/workflows/{preset_id}/run",
            {
                "confirm_billing": True,
                "user_inputs": user_inputs,
                "prompt": prompt,
                "negative_prompt": negative,
                "steps": 2,
                "width": 512,
                "height": 512,
                "seed": 24681358,
                "run_count": 1,
                "seed_after_generate": "fixed",
            },
        )
        job_stub = started.get("job") if isinstance(started.get("job"), dict) else {}
        job_id = str(job_stub.get("job_id") or "")
        if not job_id:
            raise ProbeFailure("custom workflow run returned no job id")
        job = wait_job(
            client,
            job_id,
            timeout_seconds=timeout_seconds,
            safety_monitor=safety_monitor,
            phase="custom_safe_gguf_workflow",
        )
        artifacts, image_records = validate_site_job_outputs(
            client,
            job,
            out_dir=out_dir,
            label="custom_workflow",
        )
        workflow_run_id = int(
            (job.get("result") or {}).get("workflow_run_id")
            or started.get("workflow_run_id")
            or 0
        )
        if workflow_run_id <= 0:
            raise ProbeFailure("custom workflow completed without workflow_run_id")
        input_cleanup_validation = validate_workflow_input_cleanup(started, job)
        if input_cleanup_validation.get("ok") is not True:
            raise ProbeFailure(
                f"custom workflow input cleanup was not exact: {input_cleanup_validation}"
            )
        terminal_result = job.get("result") if isinstance(job.get("result"), dict) else {}
        result_payload = {
            "ok": True,
            "source_preset_id": source_id,
            "preset_id": preset_id,
            "workflow_run_id": workflow_run_id,
            "job_id": job_id,
            "terminal_status": job.get("status"),
            "safe_profile_id": safe_selection["profile_id"],
            "safe_variant_id": safe_selection["variant_id"],
            "safe_gguf_file": safe_selection["gguf_file"],
            "safe_vae_override": safe_vae,
            "workflow_sha256": workflow_sha256,
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
            "media_remap_run_id": started.get("media_remap_run_id") or "",
            "input_assignment_count": terminal_result.get("input_assignment_count"),
            "input_cleanup": terminal_result.get("input_cleanup") or {},
            "input_cleanup_validation": input_cleanup_validation,
            "final_model_safety": terminal_result.get("final_model_safety") or {},
            "final_model_safety_validation": job.get("_final_model_safety_validation") or {},
        }
    except Exception as exc:
        primary_error = exc
    finally:
        if preset_id > 0:
            try:
                deleted = _expect_json(client, "DELETE", f"/api/comfyui/workflows/{preset_id}")
                delete_status, missing = client.json("GET", f"/api/comfyui/workflows/{preset_id}")
                if delete_status not in {403, 404} or missing.get("ok") is not False:
                    raise ProbeFailure(
                        f"custom workflow still exists after delete: HTTP {delete_status}, {missing}"
                    )
            except Exception as cleanup_exc:
                if primary_error is None:
                    primary_error = cleanup_exc
                else:
                    primary_error = ProbeFailure(
                        f"custom workflow failed ({primary_error}); cleanup also failed ({cleanup_exc})"
                    )
    if primary_error is not None:
        raise primary_error
    if result_payload is None:
        raise ProbeFailure("custom safe GGUF workflow produced no terminal result")
    result_payload["delete"] = deleted
    result_payload["delete_verified_http_status"] = delete_status
    return result_payload, image_records


def run_ai_agent_generation(
    client: WebClient,
    *,
    safe_selection: dict[str, Any],
    safety_monitor: ComfySafetyMonitor,
    out_dir: Path,
    run_id: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    catalog = _expect_json(client, "GET", "/api/ai-agent/write-tools")
    names = [str(item.get("name") or "") for item in catalog.get("tools") or [] if isinstance(item, dict)]
    if names != ["write_comfyui_generate"]:
        raise ProbeFailure(f"AI Agent effective ComfyUI tool catalog is not exact: {names}")
    response = _expect_json(
        client,
        "POST",
        "/api/ai-agent/write-tools/execute",
        {
            "tool": "write_comfyui_generate",
            "confirm": "EXECUTE",
            "arguments": {
                "prompt": f"formal AI agent generation {run_id}, blue robot tending a garden",
                "negative_prompt": "blank, black, white, corrupt, text, watermark",
                "official_workflow_id": SAFE_GGUF_WORKFLOW_ID,
                "gguf_profile": safe_selection["profile_id"],
                "gguf_variant": safe_selection["variant_id"],
                "vae": safe_selection["safe_vae_override"],
                "width": 512,
                "height": 512,
                "steps": 2,
                "cfg": 4.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "seed": 97531,
                "batch_size": 1,
                "generation_mode": "txt2img",
                "confirm_billing": True,
                "timeout_seconds": timeout_seconds,
            },
        },
    )
    if response.get("tool") != "write_comfyui_generate" or int(response.get("status") or 0) not in {200, 202}:
        raise ProbeFailure(f"AI Agent write tool response is malformed: {response}")
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    if result.get("official_workflow_id") != SAFE_GGUF_WORKFLOW_ID:
        raise ProbeFailure(f"AI Agent did not dispatch the approved GGUF workflow: {response}")
    job_stub = result.get("job") if isinstance(result.get("job"), dict) else {}
    job_id = str(job_stub.get("job_id") or "")
    if not job_id:
        raise ProbeFailure(f"AI Agent generation returned no job id: {response}")
    job = wait_job(
        client,
        job_id,
        timeout_seconds=timeout_seconds,
        safety_monitor=safety_monitor,
        phase="ai_agent_safe_gguf_generation",
    )
    artifacts, image_records = validate_site_job_outputs(
        client,
        job,
        out_dir=out_dir,
        label="ai_agent_generation",
    )
    workflow_run_id = int(
        (job.get("result") or {}).get("workflow_run_id")
        or result.get("workflow_run_id")
        or 0
    )
    if workflow_run_id <= 0:
        raise ProbeFailure("AI Agent safe GGUF generation completed without workflow_run_id")
    input_cleanup_validation = validate_workflow_input_cleanup(result, job)
    if input_cleanup_validation.get("ok") is not True:
        raise ProbeFailure(
            f"AI Agent workflow input cleanup was not exact: {input_cleanup_validation}"
        )
    terminal_result = job.get("result") if isinstance(job.get("result"), dict) else {}
    return ({
        "ok": True,
        "catalog_names": names,
        "job_id": job_id,
        "history_id": 0,
        "workflow_run_id": workflow_run_id,
        "official_workflow_id": SAFE_GGUF_WORKFLOW_ID,
        "safe_profile_id": safe_selection["profile_id"],
        "safe_variant_id": safe_selection["variant_id"],
        "safe_gguf_file": safe_selection["gguf_file"],
        "safe_vae_override": safe_selection["safe_vae_override"],
        "terminal_status": job.get("status"),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "action_policy": response.get("action_policy") or {},
        "media_remap_run_id": result.get("media_remap_run_id") or "",
        "input_assignment_count": terminal_result.get("input_assignment_count"),
        "input_cleanup": terminal_result.get("input_cleanup") or {},
        "input_cleanup_validation": input_cleanup_validation,
        "final_model_safety": terminal_result.get("final_model_safety") or {},
        "final_model_safety_validation": job.get("_final_model_safety_validation") or {},
    }, image_records)


def _browser_login(page, base_url: str, password: str) -> None:
    page.set_default_timeout(30_000)
    page.goto(base_url.rstrip("/") + "/", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_function(
        "() => typeof fetchCsrfToken === 'function' && typeof apiFetch === 'function'",
        timeout=30_000,
    )
    response = page.evaluate(
        """async ({password, timeoutMs}) => {
          const controller = new AbortController();
          let timer = null;
          const deadline = new Promise((_, reject) => {
            timer = setTimeout(() => {
              controller.abort();
              reject(new Error(`browser login deadline exceeded after ${timeoutMs}ms`));
            }, timeoutMs);
          });
          const operation = (async () => {
            await fetchCsrfToken({force: true});
            const res = await apiFetch(API + '/login', {
              method: 'POST', credentials: 'same-origin', signal: controller.signal,
              headers: {'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() || ''},
              body: JSON.stringify({username: 'root', password}),
            });
            return {status: res.status, body: await res.json().catch(() => ({}))};
          })();
          try {
            return await Promise.race([operation, deadline]);
          } finally {
            if (timer !== null) clearTimeout(timer);
          }
        }""",
        {"password": password, "timeoutMs": 30_000},
    )
    if response.get("status") != 200 or response.get("body", {}).get("ok") is not True:
        raise ProbeFailure(f"browser root login failed: {response}")
    page.goto(base_url.rstrip("/") + "/", wait_until="networkidle", timeout=30_000)


def run_workflow_ui(
    args: argparse.Namespace,
    *,
    out_dir: Path,
    safety_monitor: ComfySafetyMonitor,
    client: WebClient,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise ProbeFailure(f"Playwright is required for ComfyUI UI evidence: {exc}") from exc
    rows: list[dict[str, Any]] = []
    out_path = out_dir / "ui"
    out_path.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for label, viewport in (
            ("desktop", {"width": 1440, "height": 1050}),
            ("mobile", {"width": 390, "height": 844}),
        ):
            context = browser.new_context(ignore_https_errors=args.insecure, viewport=viewport)
            page = context.new_page()
            console_errors: list[str] = []
            page_errors: list[str] = []
            failed_requests: list[str] = []
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on("requestfailed", lambda request: failed_requests.append(f"{request.method} {request.url}: {request.failure}"))
            _browser_login(page, args.base_url, args.root_password)
            _assert_cleanup_safety(
                safety_monitor,
                client,
                phase=f"workflow_ui_{label}_login",
            )
            page.wait_for_function("() => typeof switchModuleTab === 'function'")
            page.evaluate("() => switchModuleTab('comfyui')")
            page.locator("#module-comfyui.active").wait_for(state="visible", timeout=30_000)
            page.locator("#comfyui-template-select").wait_for(state="visible", timeout=30_000)
            page.wait_for_function(
                "() => document.querySelectorAll('#comfyui-template-select option').length > 1",
                timeout=60_000,
            )
            page.locator('[data-comfyui-view="workflow"]').click()
            page.locator("#comfyui-view-workflow.active").wait_for(state="visible", timeout=15_000)
            page.wait_for_function(
                "() => document.querySelectorAll('#comfyui-workflow-official-list .comfyui-workflow-item').length > 0",
                timeout=30_000,
            )
            _assert_cleanup_safety(
                safety_monitor,
                client,
                phase=f"workflow_ui_{label}_module",
            )
            main_metrics = page.evaluate(
                """() => ({
                  innerWidth: window.innerWidth,
                  bodyWidth: document.body.scrollWidth,
                  docWidth: document.documentElement.scrollWidth,
                  options: document.querySelectorAll('#comfyui-template-select option').length,
                  official: document.querySelectorAll('#comfyui-workflow-official-list .comfyui-workflow-item').length,
                  visualLinkVisible: !!document.querySelector('#comfyui-workflow-open-visual-btn')?.getClientRects().length,
                })"""
            )
            main_shot = out_path / f"{label}_workflow_module.png"
            page.screenshot(path=str(main_shot), full_page=True)
            page.goto(args.base_url.rstrip("/") + "/comfyui-workflow-editor.html", wait_until="networkidle")
            page.locator(".wf-node").first.wait_for(state="visible", timeout=30_000)
            editor_metrics = page.evaluate(
                """() => ({
                  innerWidth: window.innerWidth,
                  bodyWidth: document.body.scrollWidth,
                  docWidth: document.documentElement.scrollWidth,
                  nodes: document.querySelectorAll('.wf-node').length,
                  edges: document.querySelectorAll('.edge-path').length,
                  validationText: document.querySelector('#validationPanel')?.textContent?.trim().slice(0, 1000) || '',
                })"""
            )
            editor_shot = out_path / f"{label}_visual_editor.png"
            page.screenshot(path=str(editor_shot), full_page=True)
            _assert_cleanup_safety(
                safety_monitor,
                client,
                phase=f"workflow_ui_{label}_editor",
            )
            overflow = max(int(main_metrics["bodyWidth"]), int(main_metrics["docWidth"])) > int(main_metrics["innerWidth"]) + 1
            editor_overflow = max(int(editor_metrics["bodyWidth"]), int(editor_metrics["docWidth"])) > int(editor_metrics["innerWidth"]) + 1
            row = {
                "label": label,
                "main": main_metrics,
                "editor": editor_metrics,
                "main_screenshot": str(main_shot),
                "editor_screenshot": str(editor_shot),
                "console_errors": console_errors,
                "page_errors": page_errors,
                "failed_requests": failed_requests,
                "overflow": overflow,
                "editor_overflow": editor_overflow,
            }
            row["ok"] = bool(
                main_metrics["options"] > 1
                and main_metrics["official"] == len(SYSTEM_WORKFLOW_IDS)
                and main_metrics["visualLinkVisible"]
                and editor_metrics["nodes"] >= 7
                and editor_metrics["edges"] >= 8
                and not overflow
                and not editor_overflow
                and not console_errors
                and not page_errors
                and not failed_requests
            )
            rows.append(row)
            context.close()
            row["context_closed"] = True
        browser.close()
    if not rows or not all(item["ok"] for item in rows):
        raise ProbeFailure(f"desktop/mobile ComfyUI workflow UI failed: {rows}")
    return {"ok": True, "browser_closed": True, "rows": rows}


def run_offline_failure(
    client: WebClient,
    args: argparse.Namespace,
    *,
    model: str,
    target_url: str,
    out_dir: Path,
    safety_monitor: ComfySafetyMonitor,
) -> dict[str, Any]:
    offline_url = "http://127.0.0.1:9"
    changed = _expect_json(
        client,
        "PUT",
        "/api/admin/settings",
        {
            "comfyui_connection_mode": "remote",
            "comfyui_remote_api_url": offline_url,
            "dangerous_confirm": ["comfyui_connection_mode", "comfyui_remote_api_url"],
        },
    )
    status_code, status_payload = client.json("GET", "/api/comfyui/status")
    workflows_code, workflows_payload = client.json("GET", "/api/comfyui/workflows")
    generate_code, generate_payload = client.json(
        "POST",
        "/api/comfyui/generate",
        {
            "generation_mode": "txt2img",
            "model": model,
            "prompt": "formal offline dependency visibility",
            "negative_prompt": "",
            "width": 512,
            "height": 512,
            "steps": 1,
            "cfg": 3,
            "seed": 123,
            "batch_size": 1,
            "sampler_name": "euler",
            "scheduler": "normal",
            "confirm_billing": True,
        },
    )
    sync_failure = generate_code >= 400 and generate_payload.get("ok") is False
    terminal_failure: dict[str, Any] | None = None
    if not sync_failure:
        job_stub = generate_payload.get("job") if isinstance(generate_payload.get("job"), dict) else {}
        job_id = str(job_stub.get("job_id") or "")
        if not job_id:
            raise ProbeFailure(f"offline generation neither failed nor returned a job: HTTP {generate_code}, {generate_payload}")
        terminal_failure = wait_job(
            client,
            job_id,
            timeout_seconds=120,
            safety_monitor=safety_monitor,
            phase="offline_dependency_failure",
        )
        if str(terminal_failure.get("status") or "").lower() not in {"error", "failed", "cancelled"}:
            raise ProbeFailure(f"offline generation unexpectedly succeeded: {terminal_failure}")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise ProbeFailure(f"Playwright is required for offline UI evidence: {exc}") from exc
    screenshot = out_dir / "ui/offline_dependency_visible.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(ignore_https_errors=args.insecure, viewport={"width": 1280, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        _browser_login(page, args.base_url, args.root_password)
        _assert_cleanup_safety(
            safety_monitor,
            client,
            phase="offline_failure_ui_login",
        )
        page.evaluate("() => switchModuleTab('comfyui')")
        page.locator("#module-comfyui.active").wait_for(state="visible", timeout=30_000)
        page.evaluate(
            """async () => {
              let timer = null;
              const deadline = new Promise((_, reject) => {
                timer = setTimeout(() => reject(new Error('offline model refresh deadline exceeded')), 20000);
              });
              try {
                return await Promise.race([
                  loadComfyuiModels({forceRefresh: true}).catch(() => null),
                  deadline,
                ]);
              } finally {
                if (timer !== null) clearTimeout(timer);
              }
            }"""
        )
        page.wait_for_timeout(1500)
        status_text = page.locator("#comfyui-status").inner_text().strip()
        page.locator("#module-comfyui").screenshot(path=str(screenshot))
        _assert_cleanup_safety(
            safety_monitor,
            client,
            phase="offline_failure_ui_terminal",
        )
        browser.close()
    browser_closed = True
    restored = _expect_json(
        client,
        "PUT",
        "/api/admin/settings",
        {
            "comfyui_connection_mode": "remote",
            "comfyui_remote_api_url": target_url,
            "dangerous_confirm": ["comfyui_connection_mode", "comfyui_remote_api_url"],
        },
    )
    live_status = _expect_json(client, "GET", "/api/comfyui/status")
    visible_failure = bool(
        status_text
        and any(token in status_text.lower() for token in ("失敗", "無法", "未連線", "錯誤", "offline", "error", "failed"))
    )
    ok = bool(
        changed.get("ok") is True
        and status_payload.get("available") is not True
        and status_payload.get("available") is False
        and workflows_code == 200
        and workflows_payload.get("ok") is True
        and bool(str(workflows_payload.get("dependency_warning") or "").strip())
        and (sync_failure or terminal_failure)
        and visible_failure
        and not page_errors
        and live_status.get("available") is True
        and restored.get("ok") is True
    )
    result = {
        "ok": ok,
        "offline_url": offline_url,
        "status_http": status_code,
        "status": status_payload,
        "workflows_http": workflows_code,
        "dependency_warning": workflows_payload.get("dependency_warning") or "",
        "generation_http": generate_code,
        "generation": generate_payload,
        "terminal_failure": terminal_failure,
        "ui_status_text": status_text,
        "ui_screenshot": str(screenshot),
        "page_errors": page_errors,
        "browser_closed": browser_closed,
        "restored_available": live_status.get("available"),
    }
    if not ok:
        raise ProbeFailure(f"offline/dependency failure was not fully visible: {result}")
    return result


def cleanup_history(
    client: WebClient,
    *,
    baseline: dict[str, dict[str, Any]],
    safety_monitor: ComfySafetyMonitor | None = None,
) -> dict[str, Any]:
    before, _ = history_inventory(client)
    new_ids = sorted(set(before) - set(baseline))
    rows: list[dict[str, Any]] = []
    for history_id in new_ids:
        item = before[history_id]
        if history_id.startswith("workflow-"):
            run_id = int(item.get("workflow_run_id") or history_id.split("-", 1)[1])
            path = f"/api/comfyui/workflow-runs/{run_id}"
        elif history_id.isdigit():
            path = f"/api/comfyui/history/{int(history_id)}"
        else:
            rows.append({"id": history_id, "ok": False, "error": "unknown history id format"})
            continue
        status, payload = client.json("DELETE", path)
        rows.append({"id": history_id, "path": path, "http_status": status, "ok": status == 200 and payload.get("ok") is True})
        _assert_cleanup_safety(
            safety_monitor,
            client,
            phase="history_row_cleanup",
        )
    after, _ = history_inventory(client)
    exact = set(after) == set(baseline)
    return {
        "ok": exact and all(item.get("ok") for item in rows),
        "baseline_ids": sorted(baseline),
        "new_ids": new_ids,
        "delete_rows": rows,
        "after_ids": sorted(after),
        "exact": exact,
    }


def contract_status(report: dict[str, Any]) -> dict[str, bool]:
    sections = report.get("sections") if isinstance(report.get("sections"), dict) else {}
    return {
        "real_backend_required": bool(
            sections.get("real_backend", {}).get("ok")
            and sections.get("safety", {}).get("ok")
        ),
        "feature_probe": bool(sections.get("feature_probe", {}).get("ok")),
        "official_templates_execute": bool(
            sections.get("dependency_preflight", {}).get("ok")
            and sections.get("official_templates", {}).get("ok")
        ),
        "custom_workflow_create_import_run_output_delete": bool(sections.get("custom_workflow", {}).get("ok")),
        "ai_agent_generation_terminal_output": bool(sections.get("ai_agent_generation", {}).get("ok")),
        "desktop_mobile_workflow_ui": bool(sections.get("workflow_ui", {}).get("ok")),
        "offline_and_dependency_failure_visible": bool(sections.get("offline_failure", {}).get("ok")),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run strict formal ComfyUI workflow evidence against an isolated target.")
    parser.add_argument("--base-url", required=True)
    add_root_password_argument(parser)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--feature-timeout", type=int, default=900)
    parser.add_argument("--official-timeout", type=int, default=1200)
    parser.add_argument("--safe-canary-timeout", type=int, default=600)
    parser.add_argument("--safe-gguf-max-bytes", type=int, default=2 * 1024 * 1024 * 1024)
    parser.add_argument("--safety-min-mem-available-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--safety-min-disk-free-bytes", type=int, default=20 * 1024 * 1024 * 1024)
    parser.add_argument("--safety-max-queue-depth", type=int, default=1)
    parser.add_argument("--safety-cancel-grace-seconds", type=int, default=45)
    parser.add_argument(
        "--safe-canary-only",
        action="store_true",
        help="Run the allowlisted terminal GGUF canary and cleanup only; always remains non-formal because mandatory sections are absent.",
    )
    parser.add_argument("--insecure", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir).expanduser().resolve()
    if out_dir == REPO_ROOT or REPO_ROOT in out_dir.parents:
        raise SystemExit("--out-dir must be outside the frozen source tree")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit("--out-dir must be absent or empty; formal artifacts cannot be overwritten")
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": utc_now(),
        "base_url": args.base_url,
        "comfyui_url": "",
        "sections": {},
        "contract": {},
        "cleanup": {},
        "errors": [],
        "ok": False,
    }
    client: WebClient | None = None
    snapshot: dict[str, Any] | None = None
    baseline_history: dict[str, dict[str, Any]] = {}
    baseline_workflows: set[int] = set()
    baseline_history_captured = False
    baseline_workflows_captured = False
    image_records: list[dict[str, Any]] = []
    safety_monitor: ComfySafetyMonitor | None = None
    try:
        if int(args.safe_gguf_max_bytes) > SAFE_MODEL_MAX_FILE_BYTES:
            raise ProbeFailure("--safe-gguf-max-bytes cannot exceed the immutable 2 GiB cap")
        if int(args.safety_min_mem_available_bytes) < GIB:
            raise ProbeFailure("--safety-min-mem-available-bytes cannot be below 1 GiB")
        if int(args.safety_min_disk_free_bytes) < 20 * GIB:
            raise ProbeFailure("--safety-min-disk-free-bytes cannot be below 20 GiB")
        if not 0 <= int(args.safety_max_queue_depth) <= 1:
            raise ProbeFailure("--safety-max-queue-depth must be 0 or 1")
        if not 15 <= int(args.safety_cancel_grace_seconds) <= 60:
            raise ProbeFailure("--safety-cancel-grace-seconds must be between 15 and 60")
        comfyui_url = require_campaign_comfyui_url()
        report["comfyui_url"] = comfyui_url
        models_root = require_comfyui_models_root()
        early_model_safety: dict[str, Any] | None = None
        if not args.safe_canary_only:
            # This is a stat-only gate and can reject the immutable all-32
            # model plan without launching a multi-minute generation.  Keep it
            # ahead of backend probing, site mutation, and the live GGUF canary
            # so a known-unsafe inventory never consumes avoidable resources.
            early_model_safety = audit_official_workflow_model_safety(models_root)
            report["sections"]["model_safety_preflight"] = early_model_safety
            if early_model_safety.get("ok") is not True:
                raise ProbeFailure(
                    "mandatory all-32 official model safety preflight failed before live canary: "
                    f"unsafe_workflows={early_model_safety.get('unsafe_workflows')}, "
                    f"safe={early_model_safety.get('safe_workflow_count')}/"
                    f"{early_model_safety.get('expected_workflow_count')}"
                )
        backend_scope = require_backend_scope_evidence(
            comfyui_url,
            models_root=models_root,
        )
        direct = ComfyUIClient(comfyui_url, timeout=30)
        health = direct.health_check(timeout=10)
        object_info = direct.get_object_info()
        required_nodes = {"CheckpointLoaderSimple", "KSampler", "VAEDecode", "SaveImage"}
        safe_required_nodes = {
            "UnetLoaderGGUF",
            "DualCLIPLoader",
            "CLIPTextEncode",
            "EmptyLatentImage",
            "VAELoader",
        }
        missing_nodes = sorted(required_nodes - set(object_info))
        missing_safe_nodes = sorted(safe_required_nodes - set(object_info))
        if not health.get("ok") or missing_nodes or missing_safe_nodes:
            raise ProbeFailure(
                "real ComfyUI backend is incomplete: "
                f"health={health}, missing_nodes={missing_nodes}, missing_safe_nodes={missing_safe_nodes}"
            )
        report["sections"]["real_backend"] = {
            "ok": True,
            "health": health,
            "object_info_node_count": len(object_info),
            "required_nodes": sorted(required_nodes),
            "missing_nodes": missing_nodes,
            "safe_required_nodes": sorted(safe_required_nodes),
            "missing_safe_nodes": missing_safe_nodes,
            "models_root": str(models_root),
            "backend_scope": backend_scope,
        }

        client = WebClient(args.base_url, insecure=args.insecure)
        client.login("root", args.root_password)
        snapshot = settings_snapshot(client)
        baseline_history, _ = history_inventory(client)
        baseline_history_captured = True
        baseline_workflows, _ = workflow_inventory(client)
        baseline_workflows_captured = True
        report["snapshot"] = {
            "settings": snapshot,
            "history_ids": sorted(baseline_history),
            "workflow_ids": sorted(baseline_workflows),
        }
        report["configuration"] = configure_formal_target(client, comfyui_url=comfyui_url)
        connection = _expect_json(
            client,
            "POST",
            "/api/root/comfyui/test-connection",
            {
                "connection_mode": "remote",
                "comfyui_connection_mode": "remote",
                "comfyui_remote_api_url": comfyui_url,
            },
        )
        if connection.get("connected") is False:
            raise ProbeFailure(f"site-side ComfyUI connection test failed: {connection}")

        safety_monitor = ComfySafetyMonitor(
            direct,
            sample_path=out_dir / "resource_samples/comfyui_safety_samples.jsonl",
            artifact_root=out_dir,
            min_mem_available_bytes=args.safety_min_mem_available_bytes,
            min_disk_free_bytes=args.safety_min_disk_free_bytes,
            max_queue_depth=args.safety_max_queue_depth,
            cancel_grace_seconds=args.safety_cancel_grace_seconds,
            backend_scope=backend_scope,
        )
        report["sections"]["safety"] = {
            "ok": False,
            "allowlist": [dict(item) for item in SAFE_GGUF_ALLOWLIST],
            "max_gguf_size_bytes": int(args.safe_gguf_max_bytes),
            "preflight": safety_monitor.assert_preflight(),
        }
        safe_selection = select_safe_gguf_profile(
            client,
            expected_comfyui_url=comfyui_url,
            max_size_bytes=args.safe_gguf_max_bytes,
            models_root=models_root,
        )
        report["sections"]["safety"]["selection"] = safe_selection
        canary, canary_images = run_safe_gguf_canary(
            client,
            selection=safe_selection,
            safety_monitor=safety_monitor,
            out_dir=out_dir,
            run_id=run_id[:12],
            timeout_seconds=args.safe_canary_timeout,
        )
        image_records.extend(canary_images)
        report["sections"]["safety"]["canary"] = canary
        safety_summary = safety_monitor.summary()
        report["sections"]["safety"]["monitor"] = safety_summary
        report["sections"]["safety"]["ok"] = bool(
            canary.get("ok") is True
            and canary.get("terminal_status") == "completed"
            and int(canary.get("artifact_count") or 0) > 0
            and safety_summary.get("samples_complete") is True
            and float(safety_summary.get("field_completeness_ratio") or 0) == 1.0
            and not safety_summary.get("collector_errors")
            and not safety_summary.get("hard_stop_samples")
            and safety_summary.get("sample_gap_within_30_seconds") is True
        )
        if report["sections"]["safety"]["ok"] is not True:
            raise ProbeFailure(f"safe GGUF canary evidence is incomplete: {report['sections']['safety']}")

        child_env = dict(os.environ)
        child_env["HACKME_PROBE_ROOT_PASSWORD"] = args.root_password
        child_env["HACKME_CAMPAIGN_COMFYUI_API_URL"] = comfyui_url
        if args.safe_canary_only:
            report["execution_mode"] = "safe_canary_only_non_formal"
            report["formal_blocker"] = (
                "safe-canary-only does not execute mandatory feature, all-32 official, custom, "
                "AI Agent, UI, or offline-failure sections"
            )
        else:
            dependency_preflight = run_mandatory_dependency_preflight(
                args,
                comfyui_url=comfyui_url,
                safe_selection=safe_selection,
                safety_monitor=safety_monitor,
                client=client,
                child_env=child_env,
                out_dir=out_dir,
                models_root=models_root,
                model_safety=early_model_safety,
            )
            report["sections"]["dependency_preflight"] = dependency_preflight
            if dependency_preflight.get("ok") is not True:
                raise ProbeFailure(
                    "mandatory all-32 official dependency preflight failed closed: "
                    f"missing_workflows={dependency_preflight.get('missing_workflows')}, "
                    f"unexpected_workflows={dependency_preflight.get('unexpected_workflows')}, "
                    f"dependency_failures={json.dumps(dependency_preflight.get('dependency_failures') or {}, ensure_ascii=False)}, "
                    f"unsafe_model_workflows={dependency_preflight.get('model_safety', {}).get('unsafe_workflows')}, "
                    f"feature_checkpoint={dependency_preflight.get('feature_checkpoint')}"
                )
            report["sections"]["feature_probe"] = run_feature_probe(
                args,
                client=client,
                safety_monitor=safety_monitor,
                comfyui_url=comfyui_url,
                child_env=child_env,
                out_dir=out_dir,
                feature_checkpoint=dependency_preflight["feature_checkpoint"],
            )
            feature_history, _ = history_inventory(client)
            expected_feature_ids = {
                str(item) for item in report["sections"]["feature_probe"]["created_history_ids"]
            }
            actual_feature_new_ids = {
                key
                for key in feature_history
                if key not in baseline_history and not key.startswith("workflow-")
            }
            if actual_feature_new_ids != expected_feature_ids:
                raise ProbeFailure(
                    "feature probe history inventory is not exact: "
                    f"expected={sorted(expected_feature_ids)}, actual={sorted(actual_feature_new_ids)}"
                )
            feature_new = [feature_history[key] for key in sorted(expected_feature_ids, key=int)]
            feature_outputs = 0
            for item in feature_new:
                result = item.get("result") if isinstance(item.get("result"), dict) else {}
                images = result.get("images") if isinstance(result.get("images"), list) else []
                for image in images:
                    if not isinstance(image, dict) or not isinstance(image.get("image_ref"), dict):
                        raise ProbeFailure("feature history contains an output without image_ref")
                    preview = _expect_json(client, "POST", "/api/comfyui/image-preview", {"image_ref": image["image_ref"]})
                    output = preview.get("image") if isinstance(preview.get("image"), dict) else {}
                    mime, data = decode_data_url(str(output.get("data_url") or ""))
                    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".bin")
                    artifact = write_artifact(out_dir, f"outputs/feature_{feature_outputs + 1:02d}{suffix}", data, mime_type=mime)
                    image_records.append({"image_ref": image["image_ref"], "prompt_id": result.get("prompt_id") or "", "artifact": artifact})
                    feature_outputs += 1
            if feature_outputs < 6:
                raise ProbeFailure(f"strict feature probe produced only {feature_outputs} decodable image outputs")
            report["sections"]["feature_probe"]["decoded_output_count"] = feature_outputs
            report["sections"]["feature_probe"]["history_inventory_exact"] = True

            official = run_official_templates(
                args,
                client=client,
                safety_monitor=safety_monitor,
                safe_selection=safe_selection,
                child_env=child_env,
                out_dir=out_dir,
            )
            image_records.extend(official.pop("_image_records", []))
            report["sections"]["official_templates"] = official
            custom, custom_images = run_custom_workflow(
                client,
                safe_selection=safe_selection,
                safety_monitor=safety_monitor,
                out_dir=out_dir,
                run_id=run_id[:12],
                timeout_seconds=args.feature_timeout,
            )
            image_records.extend(custom_images)
            report["sections"]["custom_workflow"] = custom
            agent, agent_images = run_ai_agent_generation(
                client,
                safe_selection=safe_selection,
                safety_monitor=safety_monitor,
                out_dir=out_dir,
                run_id=run_id[:12],
                timeout_seconds=args.feature_timeout,
            )
            image_records.extend(agent_images)
            report["sections"]["ai_agent_generation"] = agent
            report["sections"]["workflow_ui"] = run_workflow_ui(
                args,
                out_dir=out_dir,
                safety_monitor=safety_monitor,
                client=client,
            )
            report["sections"]["offline_failure"] = run_offline_failure(
                client,
                args,
                model=str(dependency_preflight["feature_checkpoint"]["checkpoint"]["checkpoint"]),
                target_url=comfyui_url,
                out_dir=out_dir,
                safety_monitor=safety_monitor,
            )
    except Exception as exc:
        report["errors"].append({
            "type": exc.__class__.__name__,
            "message": str(exc),
            "at": utc_now(),
        })
    finally:
        if safety_monitor is not None:
            final_safety: dict[str, Any]
            try:
                final_sample = safety_monitor.sample(
                    "formal_cleanup_entry",
                    allowed_queue_depth=0,
                )
                final_unsafe = bool(
                    final_sample.get("missing_fields")
                    or final_sample.get("collector_errors")
                    or final_sample.get("hard_limit_state", {}).get("ok") is not True
                )
                final_abort = (
                    _safe_abort_site_work(
                        safety_monitor,
                        client,
                        reason="formal_final_cleanup_hard_stop",
                    )
                    if final_unsafe
                    else None
                )
                final_safety = {
                    "ok": not final_unsafe,
                    "sample_index": len(safety_monitor.samples) - 1,
                    "queue_empty_verified": (
                        final_sample.get("backend", {}).get("queue_depth") == 0
                    ),
                    "abort": final_abort,
                }
            except Exception as exc:
                final_abort = _safe_abort_site_work(
                    safety_monitor,
                    client,
                    reason="formal_final_cleanup_collector_exception",
                )
                final_safety = {
                    "ok": False,
                    "error": f"final safety collector raised {exc.__class__.__name__}: {exc}",
                    "abort": final_abort,
                }
            report["cleanup"]["safety_entry"] = final_safety
            if final_safety.get("ok") is not True:
                report["errors"].append({
                    "type": "ProbeFailure",
                    "message": f"final ComfyUI safety state was not clean: {final_safety}",
                    "at": utc_now(),
                })
            safety_section = report.get("sections", {}).setdefault("safety", {})
            safety_section["monitor"] = safety_monitor.summary()
            safety_section["ok"] = bool(
                safety_section.get("canary", {}).get("ok") is True
                and safety_section.get("canary", {}).get("terminal_status") == "completed"
                and int(safety_section.get("canary", {}).get("artifact_count") or 0) > 0
                and safety_section["monitor"].get("samples_complete") is True
                and float(safety_section["monitor"].get("field_completeness_ratio") or 0) == 1.0
                and not safety_section["monitor"].get("collector_errors")
                and not safety_section["monitor"].get("hard_stop_samples")
                and safety_section["monitor"].get("sample_gap_within_30_seconds") is True
                and all(item.get("ok") is True for item in safety_section["monitor"].get("abort_events") or [])
                and final_safety.get("ok") is True
            )
        if client is not None:
            try:
                report["cleanup"]["discard"] = discard_images(
                    client,
                    image_records,
                    safety_monitor=safety_monitor,
                )
            except Exception as exc:
                report["cleanup"]["discard_error"] = str(exc)
            if baseline_history_captured:
                try:
                    report["cleanup"]["history"] = cleanup_history(
                        client,
                        baseline=baseline_history,
                        safety_monitor=safety_monitor,
                    )
                except Exception as exc:
                    report["cleanup"]["history"] = {"ok": False, "error": str(exc)}
            else:
                report["cleanup"]["history"] = {
                    "ok": False,
                    "error": "baseline history inventory was not captured; no destructive cleanup attempted",
                }
            if baseline_workflows_captured:
                try:
                    final_workflows, _ = workflow_inventory(client)
                    report["cleanup"]["workflow_inventory"] = {
                        "ok": final_workflows == baseline_workflows,
                        "baseline_ids": sorted(baseline_workflows),
                        "after_ids": sorted(final_workflows),
                        "unexpected": sorted(final_workflows - baseline_workflows),
                        "missing": sorted(baseline_workflows - final_workflows),
                    }
                except Exception as exc:
                    report["cleanup"]["workflow_inventory"] = {"ok": False, "error": str(exc)}
            else:
                report["cleanup"]["workflow_inventory"] = {
                    "ok": False,
                    "error": "baseline workflow inventory was not captured",
                }
            if snapshot is not None:
                try:
                    report["cleanup"]["settings_restore"] = restore_snapshot(client, snapshot)
                except Exception as exc:
                    report["cleanup"]["settings_restore"] = {"ok": False, "error": str(exc)}
        if safety_monitor is not None:
            try:
                post_cleanup_sample = safety_monitor.sample(
                    "formal_final_cleanup_post_restore",
                    allowed_queue_depth=0,
                )
                post_cleanup_unsafe = bool(
                    post_cleanup_sample.get("missing_fields")
                    or post_cleanup_sample.get("collector_errors")
                    or post_cleanup_sample.get("hard_limit_state", {}).get("ok") is not True
                )
                post_cleanup_abort = (
                    _safe_abort_site_work(
                        safety_monitor,
                        client,
                        reason="formal_post_cleanup_hard_stop",
                    )
                    if post_cleanup_unsafe
                    else None
                )
                post_cleanup_safety = {
                    "ok": not post_cleanup_unsafe,
                    "sample_index": len(safety_monitor.samples) - 1,
                    "queue_empty_verified": (
                        post_cleanup_sample.get("backend", {}).get("queue_depth") == 0
                    ),
                    "abort": post_cleanup_abort,
                }
            except Exception as exc:
                post_cleanup_abort = _safe_abort_site_work(
                    safety_monitor,
                    client,
                    reason="formal_post_cleanup_collector_exception",
                )
                post_cleanup_safety = {
                    "ok": False,
                    "error": (
                        f"post-cleanup safety collector raised {exc.__class__.__name__}: {exc}"
                    ),
                    "abort": post_cleanup_abort,
                }
            report["cleanup"]["safety_final"] = post_cleanup_safety
            if post_cleanup_safety.get("ok") is not True:
                report["errors"].append({
                    "type": "ProbeFailure",
                    "message": (
                        f"post-cleanup ComfyUI safety state was not clean: {post_cleanup_safety}"
                    ),
                    "at": utc_now(),
                })
            safety_section = report.get("sections", {}).setdefault("safety", {})
            safety_section["monitor"] = safety_monitor.summary()
            safety_section["ok"] = bool(
                safety_section.get("canary", {}).get("ok") is True
                and safety_section.get("canary", {}).get("terminal_status") == "completed"
                and int(safety_section.get("canary", {}).get("artifact_count") or 0) > 0
                and safety_section["monitor"].get("samples_complete") is True
                and float(safety_section["monitor"].get("field_completeness_ratio") or 0) == 1.0
                and not safety_section["monitor"].get("collector_errors")
                and not safety_section["monitor"].get("hard_stop_samples")
                and safety_section["monitor"].get("sample_gap_within_30_seconds") is True
                and all(
                    item.get("ok") is True
                    for item in safety_section["monitor"].get("abort_events") or []
                )
                and report.get("cleanup", {}).get("safety_entry", {}).get("ok") is True
                and post_cleanup_safety.get("ok") is True
            )
        cleanup_parts = (
            report.get("cleanup", {}).get("history", {}).get("ok") is True,
            report.get("cleanup", {}).get("workflow_inventory", {}).get("ok") is True,
            report.get("cleanup", {}).get("settings_restore", {}).get("ok") is True,
            all(item.get("ok") for item in report.get("cleanup", {}).get("discard", [])),
            "discard_error" not in report.get("cleanup", {}),
            report.get("cleanup", {}).get("safety_final", {}).get("ok") is True,
        )
        discard_rows = report.get("cleanup", {}).get("discard", [])
        report["cleanup"]["retained_remote_output_allowlist"] = []
        report["cleanup"]["exact"] = all(cleanup_parts)
        report["contract"] = contract_status(report)
        report["finished_at"] = utc_now()
        report["ok"] = bool(
            not report["errors"]
            and report["contract"]
            and all(report["contract"].values())
            and report["cleanup"].get("exact") is True
        )
        report_path = out_dir / "formal_comfyui_workflows_probe.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        artifact_rows = []
        for artifact_path in sorted(path for path in out_dir.rglob("*") if path.is_file() and path.name != "artifact_index.json"):
            artifact_rows.append({
                "path": str(artifact_path),
                "relative_path": artifact_path.relative_to(out_dir).as_posix(),
                "size_bytes": artifact_path.stat().st_size,
                "sha256": sha256_file(artifact_path),
            })
        manifest = {
            "schema_version": "hackme.formal-comfyui-workflows-artifact-index/v1",
            "run_id": run_id,
            "report": {
                "path": str(report_path),
                "size_bytes": report_path.stat().st_size,
                "sha256": sha256_file(report_path),
            },
            "artifact_count": len(artifact_rows),
            "artifacts": artifact_rows,
        }
        (out_dir / "artifact_index.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({
            "ok": report["ok"],
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "contract": report["contract"],
            "cleanup_exact": report["cleanup"].get("exact"),
            "report": str(report_path),
            "errors": report["errors"],
        }, ensure_ascii=False), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
