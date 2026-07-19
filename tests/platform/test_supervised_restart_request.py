from __future__ import annotations

import json
from pathlib import Path

import pytest

from routes.system_admin import write_supervised_restart_request


def test_supervised_restart_request_is_private_durable_and_non_overwriting(tmp_path: Path) -> None:
    root = tmp_path / "restart-requests"
    target = root / "request.json"

    receipt = write_supervised_restart_request(
        reason="formal AI Agent restart",
        delay_seconds=1.25,
        request_path=target,
        request_root=root,
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == receipt
    assert payload["schema_version"] == "hackme.supervised-restart-request/v1"
    assert payload["requesting_pid"] > 0
    assert len(payload["nonce"]) == 32
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_supervised_restart_request(
            reason="must not overwrite",
            delay_seconds=0,
            request_path=target,
            request_root=root,
        )


def test_supervised_restart_request_rejects_escape_and_root_scope(tmp_path: Path) -> None:
    root = tmp_path / "allowed"

    with pytest.raises(ValueError, match="escapes"):
        write_supervised_restart_request(
            reason="escape",
            delay_seconds=0,
            request_path=tmp_path / "outside.json",
            request_root=root,
        )
    with pytest.raises(ValueError, match="escapes"):
        write_supervised_restart_request(
            reason="root",
            delay_seconds=0,
            request_path=tmp_path / "request.json",
            request_root="/",
        )
