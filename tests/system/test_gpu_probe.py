import os

from services.system.gpu_probe import find_nvidia_smi


def test_find_nvidia_smi_uses_override_path(tmp_path):
    smi = tmp_path / "nvidia-smi"
    smi.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(smi, 0o755)

    assert find_nvidia_smi({"NVIDIA_SMI_PATH": str(smi)}, which=lambda _: None, candidates=()) == str(smi)


def test_find_nvidia_smi_falls_back_to_wsl_candidate(tmp_path):
    smi = tmp_path / "usr" / "lib" / "wsl" / "lib" / "nvidia-smi"
    smi.parent.mkdir(parents=True)
    smi.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(smi, 0o755)

    assert find_nvidia_smi({}, which=lambda _: None, candidates=(str(smi),)) == str(smi)


def test_find_nvidia_smi_rejects_missing_override(tmp_path):
    missing = tmp_path / "missing-nvidia-smi"

    assert find_nvidia_smi({"NVIDIA_SMI_PATH": str(missing)}, which=lambda _: None, candidates=()) == ""
