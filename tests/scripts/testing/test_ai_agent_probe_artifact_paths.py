import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
IMAGE_PROBES = (
    "ai_agent_multiref_controlnet_resume.py",
    "ai_agent_pose_copy_controlnet.py",
    "ai_agent_i2i_qwen_edit_fallback.py",
)
ALL_PROBES = (*IMAGE_PROBES, "ai_agent_comfyui_status_probe.py")


@pytest.mark.parametrize("name", ALL_PROBES)
def test_ai_agent_probes_help_works_without_pythonpath(tmp_path, name):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "testing" / name), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr


def test_ai_agent_probes_default_reports_outside_source_checkout():
    for name in ALL_PROBES:
        text = (ROOT / "scripts" / "testing" / name).read_text(encoding="utf-8")

        assert "test_artifact_path" in text
        assert "docs/AGENTS/reports" not in text


def test_ai_agent_image_probes_do_not_write_repo_output():
    fixture = ROOT / "scripts/testing/fixtures/ai_agent/qwen_squat_double_v_white_longhair_cat_ears.png"
    assert fixture.is_file()

    for name in IMAGE_PROBES:
        text = (ROOT / "scripts" / "testing" / name).read_text(encoding="utf-8")

        assert 'REPO_ROOT / "output' not in text
        assert "/mnt/" not in text
        assert 'parser.add_argument("--clothes-ref", required=True)' in text
        assert 'parser.add_argument("--pose-ref", required=True)' in text
        assert str(fixture.relative_to(ROOT)) in text
