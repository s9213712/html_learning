"""§18.1 first-boot seeding regression — workflows/comfyui/ → runtime/comfyui/."""

import json
import shutil
from pathlib import Path

import pytest

from services.comfyui.template.seeding import (
    list_runtime_workflows,
    runtime_comfyui_dir,
    seed_default_comfyui_workflows,
)


def _seed_source(tmp_path):
    """Build a minimal workflows/comfyui-style source tree under `tmp_path`."""
    src = tmp_path / "src" / "comfyui"
    for wid in ("alpha", "beta"):
        sub = src / wid
        sub.mkdir(parents=True)
        (sub / "workflow.json").write_text(json.dumps({wid: {"class_type": "X", "inputs": {}}}), encoding="utf-8")
        (sub / "manifest.json").write_text(json.dumps({"id": wid, "name": wid}), encoding="utf-8")
        (sub / "README.md").write_text(f"# {wid}\n", encoding="utf-8")
    return src


def test_seed_copies_when_runtime_empty(tmp_path):
    src = _seed_source(tmp_path)
    runtime = tmp_path / "rt"
    report = seed_default_comfyui_workflows(source_dir=src, runtime_root=runtime)
    assert report["source_count"] == 2
    assert sorted(report["copied"]) == ["alpha", "beta"]
    assert report["skipped"] == []
    # Both workflows present in runtime
    assert sorted(list_runtime_workflows(runtime_root=runtime)) == ["alpha", "beta"]


def test_seed_idempotent_on_subsequent_boot(tmp_path):
    src = _seed_source(tmp_path)
    runtime = tmp_path / "rt"
    seed_default_comfyui_workflows(source_dir=src, runtime_root=runtime)
    # Edit one runtime workflow as if an operator customized it
    custom_path = runtime / "comfyui" / "alpha" / "workflow.json"
    custom_path.write_text(json.dumps({"alpha": {"class_type": "CUSTOM", "inputs": {}}}), encoding="utf-8")
    report = seed_default_comfyui_workflows(source_dir=src, runtime_root=runtime)
    assert sorted(report["skipped"]) == ["alpha", "beta"]
    assert report["copied"] == []
    # Operator's edit survives — seed didn't overwrite
    body = json.loads(custom_path.read_text(encoding="utf-8"))
    assert body["alpha"]["class_type"] == "CUSTOM"


def test_seed_repairs_stale_qwen_edit_reference_image_runtime_bundle(tmp_path):
    src = tmp_path / "src" / "comfyui"
    source_bundle = src / "origin_qwen_image_edit_2509"
    source_bundle.mkdir(parents=True)
    source_workflow = {
        "78": {"class_type": "LoadImage", "inputs": {"image": "source.png", "upload": "image"}},
        "79": {
            "_meta": {"title": "Reference Pose Image"},
            "class_type": "LoadImage",
            "inputs": {"image": "reference.png", "upload": "image"},
        },
        "494": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"image1": ["78", 0], "prompt": "edit"},
        },
    }
    (source_bundle / "workflow.json").write_text(json.dumps(source_workflow), encoding="utf-8")
    (source_bundle / "manifest.json").write_text(
        json.dumps({"id": "origin_qwen_image_edit_2509", "name": "Qwen edit"}),
        encoding="utf-8",
    )

    runtime = tmp_path / "rt"
    runtime_bundle = runtime / "comfyui" / "origin_qwen_image_edit_2509"
    runtime_bundle.mkdir(parents=True)
    stale_workflow = {
        "78": {"class_type": "LoadImage", "inputs": {"image": "source.png", "upload": "image"}},
        "494": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"image1": ["78", 0], "image2": ["79", 0], "prompt": "operator tuned prompt"},
        },
    }
    (runtime_bundle / "workflow.json").write_text(json.dumps(stale_workflow), encoding="utf-8")
    (runtime_bundle / "manifest.json").write_text(
        json.dumps({"id": "origin_qwen_image_edit_2509", "name": "Qwen edit"}),
        encoding="utf-8",
    )

    report = seed_default_comfyui_workflows(source_dir=src, runtime_root=runtime)
    repaired = json.loads((runtime_bundle / "workflow.json").read_text(encoding="utf-8"))

    assert report["copied"] == []
    assert report["skipped"] == ["origin_qwen_image_edit_2509"]
    assert report["repaired"] == ["origin_qwen_image_edit_2509"]
    assert repaired["79"] == source_workflow["79"]
    assert "image2" not in repaired["494"]["inputs"]
    assert repaired["494"]["inputs"]["prompt"] == "operator tuned prompt"


def test_seed_fills_in_missing_workflows(tmp_path):
    """If a new workflow lands in source after first seed, it gets copied."""
    src = _seed_source(tmp_path)
    runtime = tmp_path / "rt"
    seed_default_comfyui_workflows(source_dir=src, runtime_root=runtime)
    # Add a third workflow at the source
    third = src / "gamma"
    third.mkdir()
    (third / "workflow.json").write_text("{}", encoding="utf-8")
    (third / "manifest.json").write_text("{}", encoding="utf-8")
    report = seed_default_comfyui_workflows(source_dir=src, runtime_root=runtime)
    assert "gamma" in report["copied"]
    assert "alpha" in report["skipped"]
    assert "beta" in report["skipped"]


def test_seed_overwrite_replaces_runtime_copy(tmp_path):
    src = _seed_source(tmp_path)
    runtime = tmp_path / "rt"
    seed_default_comfyui_workflows(source_dir=src, runtime_root=runtime)
    custom_path = runtime / "comfyui" / "alpha" / "workflow.json"
    custom_path.write_text("MUTATED", encoding="utf-8")
    report = seed_default_comfyui_workflows(source_dir=src, runtime_root=runtime, overwrite=True)
    assert sorted(report["copied"]) == ["alpha", "beta"]
    # Custom mutation overwritten with seed
    assert custom_path.read_text(encoding="utf-8").strip() != "MUTATED"


def test_seed_skips_partial_source_dirs(tmp_path):
    """A source subdir without workflow.json+manifest.json is ignored."""
    src = tmp_path / "src" / "comfyui"
    good = src / "alpha"
    good.mkdir(parents=True)
    (good / "workflow.json").write_text("{}", encoding="utf-8")
    (good / "manifest.json").write_text("{}", encoding="utf-8")
    bad = src / "broken"
    bad.mkdir()
    (bad / "README.md").write_text("partial", encoding="utf-8")
    report = seed_default_comfyui_workflows(source_dir=src, runtime_root=tmp_path / "rt")
    assert report["copied"] == ["alpha"]
    assert report["source_count"] == 1


def test_seed_repairs_corrupt_runtime_dir(tmp_path):
    """If runtime has a partial subdir (only README), seed should re-copy."""
    src = _seed_source(tmp_path)
    runtime = tmp_path / "rt"
    target = runtime / "comfyui" / "alpha"
    target.mkdir(parents=True)
    (target / "README.md").write_text("legacy partial", encoding="utf-8")
    report = seed_default_comfyui_workflows(source_dir=src, runtime_root=runtime)
    # alpha was incomplete → re-copied
    assert "alpha" in report["copied"]
    # workflow.json now present
    assert (target / "workflow.json").is_file()
    assert (target / "manifest.json").is_file()


def test_seed_reports_destination_path(tmp_path):
    src = _seed_source(tmp_path)
    runtime = tmp_path / "rt"
    report = seed_default_comfyui_workflows(source_dir=src, runtime_root=runtime)
    assert report["destination"].endswith("comfyui")
    assert report["runtime_count"] == 2


def test_runtime_comfyui_dir_honors_hackme_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HACKME_RUNTIME_DIR", str(tmp_path / "explicit-runtime"))
    target = runtime_comfyui_dir()
    assert target == tmp_path / "explicit-runtime" / "comfyui"


def test_list_runtime_workflows_returns_only_complete_dirs(tmp_path):
    src = _seed_source(tmp_path)
    runtime = tmp_path / "rt"
    seed_default_comfyui_workflows(source_dir=src, runtime_root=runtime)
    # Add a partial dir manually
    partial = runtime / "comfyui" / "incomplete"
    partial.mkdir(parents=True)
    (partial / "README.md").write_text("only readme", encoding="utf-8")
    ids = list_runtime_workflows(runtime_root=runtime)
    assert sorted(ids) == ["alpha", "beta"]
    assert "incomplete" not in ids
