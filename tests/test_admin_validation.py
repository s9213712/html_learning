import os
from pathlib import Path

from services.platform.admin_validation import (
    feature_dependency_error_payload,
    is_hhmm,
    normalize_ip_whitelist_or_none,
    parse_int_in_range,
    parse_strict_bool,
    public_relative_path,
    validate_comfyui_api_host,
    validate_comfyui_api_url,
    validate_comfyui_relative_script,
    validate_git_branch_name,
)


def test_parse_strict_bool_accepts_only_explicit_values():
    assert parse_strict_bool(True) is True
    assert parse_strict_bool("yes") is True
    assert parse_strict_bool("0") is False
    assert parse_strict_bool(" maybe ") is None
    assert parse_strict_bool(2) is None


def test_parse_int_in_range_rejects_bool_fraction_and_out_of_range():
    assert parse_int_in_range("10", 1, 20) == 10
    assert parse_int_in_range(10.0, 1, 20) == 10
    assert parse_int_in_range(True, 1, 20) is None
    assert parse_int_in_range("10.5", 1, 20) is None
    assert parse_int_in_range(30, 1, 20) is None


def test_is_hhmm_accepts_24_hour_values_only():
    assert is_hhmm("03:00") is True
    assert is_hhmm("23:59") is True
    assert is_hhmm("24:00") is False
    assert is_hhmm("3:00") is False


def test_normalize_ip_whitelist_or_none_normalizes_valid_entries():
    normalized, bad = normalize_ip_whitelist_or_none("127.0.0.1, 10.0.0.0/24")
    assert normalized == "127.0.0.1,10.0.0.0/24"
    assert bad == []

    normalized, bad = normalize_ip_whitelist_or_none("127.0.0.1, nope")
    assert normalized is None
    assert bad == ["nope"]


def test_feature_dependency_error_payload_uses_first_feature_and_all_missing_labels():
    payload = feature_dependency_error_payload(
        [
            {"feature_label": "交易", "required_label": "PointsChain"},
            {"feature_label": "交易", "required_label": "雲端硬碟"},
        ]
    )
    assert payload["ok"] is False
    assert payload["msg"] == "交易 需要先啟用：PointsChain、雲端硬碟"


def test_public_relative_path_hides_outside_base():
    base = Path("/tmp/base")
    inside = public_relative_path(base / "docs" / "README.md", base)
    outside = public_relative_path(Path("/tmp/elsewhere/file.txt"), base)
    assert inside == "docs/README.md"
    assert outside == "<outside>/file.txt"


def test_validate_comfyui_api_host_rejects_url_and_accepts_hostname():
    assert validate_comfyui_api_host("localhost") == "localhost"
    assert validate_comfyui_api_host("192.168.1.20") == "192.168.1.20"
    assert validate_comfyui_api_host("http://127.0.0.1:8192/prompt") is None


def test_validate_comfyui_api_url_supports_optional_blank_and_strict_shape():
    assert validate_comfyui_api_url("https://127.0.0.1:8192") == "https://127.0.0.1:8192"
    assert validate_comfyui_api_url("", allow_blank=True) == ""
    assert validate_comfyui_api_url("") == ""
    assert validate_comfyui_api_url("", allow_blank=False) is None
    assert validate_comfyui_api_url("http://user:pass@host:8188") is None
    assert validate_comfyui_api_url("https://host:8188/prompt") is None


def test_validate_comfyui_api_url_can_return_error_reason_for_route_message_mapping():
    assert validate_comfyui_api_url("http://user:pass@host:8188", return_error=True) == (None, "credentials")
    assert validate_comfyui_api_url("https://host:8188/prompt", return_error=True) == (None, "path")
    assert validate_comfyui_api_url("", allow_blank=False, return_error=True) == (None, "blank")


def test_validate_comfyui_relative_script_normalizes_absolute_path_within_base(tmp_path):
    base = tmp_path / "ComfyUI"
    base.mkdir()
    script = base / "run" / "start.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/bash\n", encoding="utf-8")

    assert validate_comfyui_relative_script("run/start.sh", base_dir=base) == "run/start.sh"
    assert validate_comfyui_relative_script(str(script), base_dir=base) == "run/start.sh"
    assert validate_comfyui_relative_script("../outside.sh", base_dir=base) is None


def test_validate_comfyui_relative_script_allows_secure_trusted_external_script(tmp_path, monkeypatch):
    base = tmp_path / "ComfyUI"
    base.mkdir()
    trusted = tmp_path / "ops" / "comfyui"
    trusted.mkdir(parents=True)
    script = trusted / "start.sh"
    script.write_text("#!/bin/bash\n", encoding="utf-8")
    os.chmod(script, 0o755)
    monkeypatch.setenv("COMFYUI_TRUSTED_START_SCRIPT_DIRS", str(trusted))

    assert validate_comfyui_relative_script(str(script), base_dir=base) == str(script.resolve())


def test_validate_comfyui_relative_script_rejects_writable_trusted_external_script(tmp_path, monkeypatch):
    base = tmp_path / "ComfyUI"
    base.mkdir()
    trusted = tmp_path / "ops" / "comfyui"
    trusted.mkdir(parents=True)
    script = trusted / "start.sh"
    script.write_text("#!/bin/bash\n", encoding="utf-8")
    os.chmod(script, 0o777)
    monkeypatch.setenv("COMFYUI_TRUSTED_START_SCRIPT_DIRS", str(trusted))

    assert validate_comfyui_relative_script(str(script), base_dir=base) is None


def test_validate_git_branch_name_rejects_invalid_refs():
    assert validate_git_branch_name("feature/docs") == "feature/docs"
    assert validate_git_branch_name("HEAD") is None
    assert validate_git_branch_name("../oops") is None
    assert validate_git_branch_name("branch.lock") is None
