import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / "scripts" / "testing" / "pytest_in_tmp.sh"


def _run_with_root(path: Path):
    env = os.environ.copy()
    env["PYTEST_TMP_ROOT"] = str(path)
    return subprocess.run(
        [str(WRAPPER), "-q", "tests/does_not_need_to_exist.py"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )


def test_pytest_wrapper_rejects_caller_root_outside_tmp():
    result = _run_with_root(Path("/var/tmp/hackme_web_pytest_forbidden"))

    assert result.returncode == 2
    assert "PYTEST_TMP_ROOT must resolve below /tmp" in result.stdout


def test_pytest_wrapper_rejects_run_root_inside_source_checkout():
    result = _run_with_root(ROOT)

    assert result.returncode == 2
    assert "PYTEST_TMP_ROOT must stay outside the source checkout" in result.stdout


def test_pytest_wrapper_rejects_existing_or_symlink_copy_target(tmp_path):
    target = tmp_path / "hackme_web"
    target.symlink_to(ROOT, target_is_directory=True)

    result = _run_with_root(tmp_path)

    assert result.returncode == 2
    assert "copy target already exists" in result.stdout
    assert target.is_symlink()


def test_pytest_wrapper_only_auto_removes_its_own_run_root():
    text = WRAPPER.read_text(encoding="utf-8")

    assert 'RUN_ROOT="$(mktemp -d /tmp/hackme_web_pytest_XXXXXX)"' in text
    assert 'AUTO_RUN_ROOT=1' in text
    assert '"$AUTO_RUN_ROOT" == "1"' in text
    assert "kept caller-selected tmp root" in text


def test_pytest_wrapper_keeps_runtime_and_artifacts_outside_copied_checkout():
    text = WRAPPER.read_text(encoding="utf-8")

    assert 'export HACKME_RUNTIME_DIR="$RUN_ROOT/runtime"' in text
    assert 'export HACKME_TEST_OUTPUT_ROOT="$RUN_ROOT/test_artifacts"' in text
    assert 'export TMPDIR="$RUN_ROOT/tmp"' in text
    assert 'HACKME_RUNTIME_DIR="$COPY_ROOT/runtime"' not in text
