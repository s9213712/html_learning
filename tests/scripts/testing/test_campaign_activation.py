from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.testing import campaign_activation as activation_module
from scripts.testing.campaign_activation import (
    ActivationArtifactError,
    assert_fresh_artifact_paths,
    prepare_private_directory,
    secure_read_json,
    secure_write_once_json,
)


def private_artifact_root(tmp_path: Path) -> tuple[Path, Path]:
    authority = tmp_path / "runtime"
    authority.mkdir(mode=0o755)
    directory = prepare_private_directory(
        authority / "activation",
        authority_root=authority,
    )
    return authority, directory


def test_one_shot_activation_artifact_is_private_atomic_and_immutable(
    tmp_path: Path,
) -> None:
    authority, directory = private_artifact_root(tmp_path)
    path = directory / "ready.json"
    payload = {"schema_version": "test.v1", "sequence": 1}

    digest = secure_write_once_json(path, payload, authority_root=authority)
    readback, readback_digest = secure_read_json(path, authority_root=authority)

    assert readback == payload
    assert readback_digest == digest
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_nlink == 1
    with pytest.raises(ActivationArtifactError, match="already exists"):
        secure_write_once_json(path, payload, authority_root=authority)


def test_activation_artifacts_reject_preexisting_symlink_hardlink_and_unsafe_mode(
    tmp_path: Path,
) -> None:
    authority, directory = private_artifact_root(tmp_path)
    target = directory / "target.json"
    secure_write_once_json(target, {"ok": True}, authority_root=authority)

    symlink = directory / "symlink.json"
    symlink.symlink_to(target)
    with pytest.raises(ActivationArtifactError, match="securely open"):
        secure_read_json(symlink, authority_root=authority)

    hardlink = directory / "hardlink.json"
    os.link(target, hardlink)
    with pytest.raises(ActivationArtifactError, match="link count"):
        secure_read_json(target, authority_root=authority)

    hardlink.unlink()
    os.chmod(directory, 0o755)
    with pytest.raises(ActivationArtifactError, match="parent must be owned mode 0700"):
        secure_write_once_json(
            directory / "activation.json",
            {"ok": True},
            authority_root=authority,
        )


def test_freshness_check_rejects_broken_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    authority, directory = private_artifact_root(tmp_path)
    stale = directory / "stale.json"
    stale.symlink_to(directory / "missing-target.json")

    with pytest.raises(ActivationArtifactError, match="pre-existing"):
        assert_fresh_artifact_paths([stale])


def test_secure_read_rejects_same_inode_rewrite_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, directory = private_artifact_root(tmp_path)
    path = directory / "ready.json"
    secure_write_once_json(
        path,
        {"payload": "a" * 70_000},
        authority_root=authority,
    )
    original_read = activation_module.os.read
    calls = 0

    def racing_read(descriptor: int, count: int) -> bytes:
        nonlocal calls
        data = original_read(descriptor, count)
        calls += 1
        if calls == 1:
            raw = path.read_bytes()
            path.write_bytes(raw.replace(b"a", b"b", 1))
            os.chmod(path, 0o600)
        return data

    monkeypatch.setattr(activation_module.os, "read", racing_read)

    with pytest.raises(ActivationArtifactError, match="changed during secure read"):
        secure_read_json(path, authority_root=authority)
