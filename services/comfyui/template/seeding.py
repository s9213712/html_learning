"""§18.1 first-boot seeding for runtime/comfyui/.

The repo ships a set of system workflows under workflows/comfyui/. On first
server start we copy those into the runtime tree
(``$HACKME_RUNTIME_DIR/comfyui/`` or the external XDG state directory) so:

- Operators can edit / extend templates without git churn.
- The runtime tree stays gitignored, so customizations don't pollute the
  repo state.
- A fresh checkout still gets working defaults on first boot.

Idempotent: subsequent boots are a no-op once the runtime tree has any
workflows in it. The check is per-workflow-id (not "directory exists") so
operators can drop in new bundles without us blocking.

Spec reference: docs/comfyui/COMFYUI_TEMPLATE_IMPORTER_PLAN.md §18.1.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable
import json

from services.server.runtime import default_runtime_root_path


REPO_SOURCE_DIR = Path(__file__).resolve().parents[3] / "workflows" / "comfyui"
SYSTEM_WORKFLOW_IDS = (
    "origin_audio_ace_step_15_xl_base",
    "origin_qwen_image_controlnet_2512",
    "origin_sd35_large_canny_controlnet",
    "origin_sd35_large_depth_controlnet",
    "origin_capybara_image_edit",
    "origin_qwen_image_edit_2509",
    "origin_qwen_image_edit_2509_anything2real",
    "origin_qwen_image_edit_gguf_lite",
    "origin_sdxl_checkpoint_inpaint",
    "origin_flux_fill_inpaint",
    "origin_flux_fill_inpaint_gguf_q3",
    "origin_one_click_anime_to_real",
    "origin_flux_fill_outpaint",
    "origin_flux_fill_outpaint_gguf_q3",
    "origin_anima_txt2img",
    "origin_sd35_txt2img",
    "origin_sdxl_txt2img",
    "origin_sdxl_gguf_txt2img",
    "origin_zit_txt2img",
    "origin_flux_dev_txt2img",
    "origin_qwen_image_txt2img",
    "origin_netayume_txt2img",
    "origin_compare_2checkpoints",
    "origin_multi_compare_checkpoints_test",
    "origin_sdpose_multi_person",
    "origin_sam3_segmentation",
    "origin_multi_method_upscale",
    "origin_multi_method_upscale_mode_test",
    "origin_capybara_video_edit",
    "origin_wan_vace_inpainting",
    "origin_wan22_14b_i2v_subgraphed",
    "origin_ltx23_t2v",
)


QWEN_IMAGE_EDIT_2509_REFERENCE_NODE_ID = "79"
QWEN_IMAGE_EDIT_2509_PROMPT_NODE_ID = "494"
QWEN_IMAGE_EDIT_2509_REFERENCE_IMAGE = "image_qwen_image_edit_2509_reference_image.png"


def _runtime_root() -> Path:
    """Resolve the runtime base dir without ever falling back into source."""
    env_dir = os.environ.get("HACKME_RUNTIME_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return default_runtime_root_path()


def runtime_comfyui_dir(*, runtime_root: Path | None = None) -> Path:
    """Where seeded ComfyUI workflows live at runtime."""
    base = Path(runtime_root) if runtime_root is not None else _runtime_root()
    return base / "comfyui"


def _is_complete_workflow_dir(path: Path) -> bool:
    """A workflow directory must carry workflow.json + manifest.json."""
    if not path.is_dir():
        return False
    return (path / "workflow.json").is_file() and (path / "manifest.json").is_file()


def _list_seed_candidates(source_dir: Path) -> list[Path]:
    if not source_dir.is_dir():
        return []
    return sorted(p for p in source_dir.iterdir() if _is_complete_workflow_dir(p))


def seed_default_comfyui_workflows(
    *,
    source_dir: Path | None = None,
    runtime_root: Path | None = None,
    overwrite: bool = False,
) -> dict:
    """Copy any workflows missing from the runtime tree.

    By default we never overwrite an existing runtime workflow_id — operators
    may have edited it. Pass ``overwrite=True`` to force a re-copy (used by
    the admin "reset templates" endpoint or post-upgrade migrations).

    Returns a small report dict for ops dashboards / audit log:
    ``{"source_count", "runtime_count", "copied", "skipped", "destination"}``
    """
    source = Path(source_dir) if source_dir is not None else REPO_SOURCE_DIR
    target = runtime_comfyui_dir(runtime_root=runtime_root)

    source_dirs = _list_seed_candidates(source)
    if not source_dirs:
        return {
            "source_count": 0,
            "runtime_count": _list_runtime_count(target),
            "copied": [],
            "skipped": [],
            "destination": str(target),
        }

    target.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    skipped: list[str] = []
    repaired: list[str] = []
    for src in source_dirs:
        dst = target / src.name
        if dst.exists() and _is_complete_workflow_dir(dst) and not overwrite:
            if _repair_runtime_system_workflow_if_needed(src, dst):
                repaired.append(src.name)
            skipped.append(src.name)
            continue
        # Either dst is partial/corrupt (always replace) or overwrite=True.
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        copied.append(src.name)

    return {
        "source_count": len(source_dirs),
        "runtime_count": _list_runtime_count(target),
        "copied": copied,
        "skipped": skipped,
        "repaired": repaired,
        "destination": str(target),
    }


def _repair_runtime_system_workflow_if_needed(source_dir: Path, runtime_dir: Path) -> bool:
    """Apply narrow migrations for official runtime bundles shipped by the repo.

    Runtime bundles are intentionally not overwritten on every boot because an
    operator may tune them. Some official workflow updates are structural
    compatibility fixes, though, and stale runtime copies silently drop newer
    capabilities. Keep those migrations explicit and conservative.
    """
    if source_dir.name != "origin_qwen_image_edit_2509":
        return False
    source_workflow_path = source_dir / "workflow.json"
    runtime_workflow_path = runtime_dir / "workflow.json"
    if not source_workflow_path.is_file() or not runtime_workflow_path.is_file():
        return False
    try:
        source_workflow = json.loads(source_workflow_path.read_text(encoding="utf-8"))
        runtime_workflow = json.loads(runtime_workflow_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(source_workflow, dict) or not isinstance(runtime_workflow, dict):
        return False

    source_reference_node = source_workflow.get(QWEN_IMAGE_EDIT_2509_REFERENCE_NODE_ID)
    source_prompt_node = source_workflow.get(QWEN_IMAGE_EDIT_2509_PROMPT_NODE_ID)
    if not isinstance(source_reference_node, dict) or not isinstance(source_prompt_node, dict):
        return False
    source_prompt_inputs = source_prompt_node.get("inputs")
    if not isinstance(source_prompt_inputs, dict):
        return False
    source_image2 = source_prompt_inputs.get("image2")

    runtime_prompt_node = runtime_workflow.get(QWEN_IMAGE_EDIT_2509_PROMPT_NODE_ID)
    runtime_prompt_inputs = runtime_prompt_node.get("inputs") if isinstance(runtime_prompt_node, dict) else None
    runtime_image2 = runtime_prompt_inputs.get("image2") if isinstance(runtime_prompt_inputs, dict) else None
    if (
        runtime_workflow.get(QWEN_IMAGE_EDIT_2509_REFERENCE_NODE_ID) == source_reference_node
        and isinstance(runtime_prompt_inputs, dict)
        and runtime_image2 == source_image2
    ):
        return False

    runtime_workflow[QWEN_IMAGE_EDIT_2509_REFERENCE_NODE_ID] = source_reference_node
    if not isinstance(runtime_prompt_inputs, dict):
        runtime_prompt_inputs = {}
        if isinstance(runtime_prompt_node, dict):
            runtime_prompt_node["inputs"] = runtime_prompt_inputs
    if isinstance(runtime_prompt_inputs, dict):
        if source_image2 == [QWEN_IMAGE_EDIT_2509_REFERENCE_NODE_ID, 0]:
            runtime_prompt_inputs["image2"] = source_image2
        else:
            runtime_prompt_inputs.pop("image2", None)
    runtime_workflow_path.write_text(
        json.dumps(runtime_workflow, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def _list_runtime_count(target: Path) -> int:
    if not target.exists():
        return 0
    return sum(1 for p in target.iterdir() if _is_complete_workflow_dir(p))


def list_runtime_workflows(*, runtime_root: Path | None = None) -> list[str]:
    """Workflow ids currently present at runtime/comfyui/. Used by the registry."""
    target = runtime_comfyui_dir(runtime_root=runtime_root)
    if not target.is_dir():
        return []
    return sorted(p.name for p in target.iterdir() if _is_complete_workflow_dir(p))


__all__ = [
    "REPO_SOURCE_DIR",
    "SYSTEM_WORKFLOW_IDS",
    "list_runtime_workflows",
    "runtime_comfyui_dir",
    "seed_default_comfyui_workflows",
]
