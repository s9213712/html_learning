from pathlib import Path

import pytest

from scripts.testing.windows_comfyui_http_bridge import resolve_cleanup_target


def test_resolve_cleanup_target_accepts_exact_run_scoped_input(tmp_path):
    run_id = "a" * 32
    target = resolve_cleanup_target(
        tmp_path,
        f"/view?filename=1_source.png&subfolder={run_id}&type=input",
    )

    assert target == (tmp_path / run_id / "1_source.png").resolve()


@pytest.mark.parametrize(
    "request_path",
    [
        "/view?filename=source.png&subfolder=../escape&type=input",
        f"/view?filename=../source.png&subfolder={'a' * 32}&type=input",
        f"/view?filename=source.png&subfolder={'a' * 31}&type=input",
    ],
)
def test_resolve_cleanup_target_rejects_unsafe_input_refs(tmp_path, request_path):
    with pytest.raises(ValueError):
        resolve_cleanup_target(tmp_path, request_path)


def test_resolve_cleanup_target_ignores_non_input_delete(tmp_path):
    assert resolve_cleanup_target(
        tmp_path,
        f"/view?filename=result.png&subfolder={'a' * 32}&type=output",
    ) is None
