"""Credential arguments for probes that attach to an existing web server."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable
from typing import Any


ROOT_PASSWORD_ENV_NAMES = (
    "HACKME_PROBE_ROOT_PASSWORD",
    "HACKME_ROOT_PASSWORD",
    "PENTEST_ROOT_PASSWORD",
    "PLAYWRIGHT_ROOT_PASSWORD",
    "HTML_LEARNING_ROOT_PASSWORD",
    "ROOT_PASSWORD",
)
MANAGER_PASSWORD_ENV_NAMES = (
    "HACKME_PROBE_MANAGER_PASSWORD",
    "HACKME_MANAGER_PASSWORD",
    "PENTEST_MANAGER_PASSWORD",
    "PLAYWRIGHT_MANAGER_PASSWORD",
    "HTML_LEARNING_MANAGER_PASSWORD",
    "MANAGER_PASSWORD",
)
USER_PASSWORD_ENV_NAMES = (
    "HACKME_PROBE_USER_PASSWORD",
    "HACKME_TEST_PASSWORD",
    "PENTEST_USER_PASSWORD",
    "PENTEST_TEST_PASSWORD",
    "PLAYWRIGHT_TEST_PASSWORD",
    "HTML_LEARNING_TEST_PASSWORD",
    "TEST_PASSWORD",
)

SENSITIVE_ARTIFACT_FIELDS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "password_confirm",
    "secret",
    "token",
}


def _is_sensitive_artifact_field(name: object) -> bool:
    normalized = str(name or "").strip().lower().replace("-", "_")
    return normalized in SENSITIVE_ARTIFACT_FIELDS or normalized.endswith(("_password", "_secret", "_token", "_api_key"))


def redact_artifact_data(value: Any, *, secret_values: Iterable[str] = ()) -> Any:
    """Return JSON-compatible probe evidence with credentials removed."""

    secrets = tuple(item for item in (str(raw or "") for raw in secret_values) if item)

    def redact(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: "[redacted]" if _is_sensitive_artifact_field(key) else redact(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [redact(child) for child in item]
        if isinstance(item, tuple):
            return [redact(child) for child in item]
        if isinstance(item, str):
            stripped = item.strip()
            if stripped.startswith(("{", "[")):
                try:
                    decoded = json.loads(item)
                except (TypeError, ValueError):
                    decoded = None
                if isinstance(decoded, (dict, list)):
                    return json.dumps(redact(decoded), ensure_ascii=False, separators=(",", ":"))
            for secret in secrets:
                item = item.replace(secret, "[redacted]")
            return item
        return item

    return redact(value)


def first_secret_from_env(names: Iterable[str]) -> tuple[str, str]:
    for name in names:
        value = str(os.environ.get(name, "") or "")
        if value:
            return value, name
    return "", ""


def add_password_argument(
    parser: argparse.ArgumentParser,
    option: str,
    *,
    env_names: Iterable[str],
    help_text: str,
    aliases: Iterable[str] = (),
) -> None:
    names = tuple(env_names)
    value, source = first_secret_from_env(names)
    env_hint = "/".join(names)
    parser.add_argument(
        option,
        *tuple(aliases),
        default=value,
        required=not bool(value),
        help=f"{help_text} Required unless {env_hint} is set."
        + (f" Current default source: {source}." if source else ""),
    )


def add_root_password_argument(
    parser: argparse.ArgumentParser,
    option: str = "--root-password",
    *,
    env_names: Iterable[str] = ROOT_PASSWORD_ENV_NAMES,
    help_text: str = "Root password for the existing target server.",
) -> None:
    add_password_argument(
        parser,
        option,
        env_names=env_names,
        help_text=help_text,
    )


def add_manager_password_argument(
    parser: argparse.ArgumentParser,
    option: str = "--manager-password",
    *,
    env_names: Iterable[str] = MANAGER_PASSWORD_ENV_NAMES,
    help_text: str = "Manager password for the existing target server.",
) -> None:
    add_password_argument(
        parser,
        option,
        env_names=env_names,
        help_text=help_text,
    )


def add_user_password_argument(
    parser: argparse.ArgumentParser,
    option: str = "--test-password",
    *,
    env_names: Iterable[str] = USER_PASSWORD_ENV_NAMES,
    help_text: str = "Test-user password for the existing target server.",
    aliases: Iterable[str] = (),
) -> None:
    add_password_argument(
        parser,
        option,
        env_names=env_names,
        help_text=help_text,
        aliases=aliases,
    )
