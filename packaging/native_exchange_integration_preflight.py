"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 native_exchange_integration_preflight.py                         │
│  Module: cyrene.packaging.native_exchange_integration_preflight      │
│  Role: Validate host-owned Exchange integration config before plans. │
│                                                                      │
│  模块职责：在 Package Runtime 计划写入前校验主机 Exchange 集成配置。     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

EXCHANGE_CONFIGURATION_ENVIRONMENT = Path("/etc/cyrene/exchange-integration.env")
EXCHANGE_SECRET_ENVIRONMENT = Path("/etc/cyrene/exchange-integration-secrets.env")
AUTHORITY_CONFIGURATION_ENVIRONMENT = Path("/etc/cyrene/workspace-authority.env")
CYRENE_COMMAND = Path("/usr/bin/cyrene")
MAX_ENVIRONMENT_FILE_BYTES = 64 * 1024
ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
PRODUCT_WORKLOAD_PROFILES_ENVIRONMENT_NAME = "CYRENE_AUTHORITY_PRODUCT_WORKLOAD_PROFILES_JSON"


class ExchangeIntegrationPreflightError(RuntimeError):
    """Raised when host configuration cannot support an integration workload."""


def _read_environment_file(
    path: Path,
    *,
    required: bool,
    secret: bool,
    owner_uid: int,
    selected_names: frozenset[str] | None = None,
) -> dict[str, str]:
    """Read a bounded host EnvironmentFile without executing shell syntax.

    Secret values are returned only to the caller's private environment map.
    Error messages never include an assignment value. 中文：只在内存中读取凭据。
    """

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise ExchangeIntegrationPreflightError(
                "Host Exchange integration configuration is unavailable."
            ) from None
        return {}
    except OSError:
        raise ExchangeIntegrationPreflightError(
            "Host Exchange integration environment file is unavailable."
        ) from None

    allowed_mode = stat.S_IMODE(metadata.st_mode)
    unsafe_permissions = allowed_mode & 0o077 if secret else allowed_mode & 0o022
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > MAX_ENVIRONMENT_FILE_BYTES
        or unsafe_permissions
    ):
        raise ExchangeIntegrationPreflightError(
            "Host Exchange integration environment file has unsafe ownership or permissions."
        )

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise ExchangeIntegrationPreflightError(
            "Host Exchange integration environment file is unreadable."
        ) from None

    result: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, raw_value = stripped.partition("=")
        if not separator or ENVIRONMENT_NAME.fullmatch(name) is None:
            raise ExchangeIntegrationPreflightError(
                "Host Exchange integration environment file has invalid syntax."
            )
        if name in result:
            raise ExchangeIntegrationPreflightError(
                "Host Exchange integration environment file repeats a variable."
            )
        if selected_names is not None and name not in selected_names:
            continue
        if raw_value.startswith("'"):
            if len(raw_value) < 2 or not raw_value.endswith("'") or "'" in raw_value[1:-1]:
                raise ExchangeIntegrationPreflightError(
                    "Host Exchange integration environment file has invalid quoting."
                )
            value = raw_value[1:-1]
        else:
            value = raw_value.strip()
            if any(character.isspace() for character in value) or any(
                character in value for character in "'\"\\$`"
            ):
                raise ExchangeIntegrationPreflightError(
                    "Host Exchange integration environment file has unsupported quoting."
                )
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ExchangeIntegrationPreflightError(
                "Host Exchange integration environment file has an invalid value."
            )
        result[name] = value
    return result


def run_exchange_integration_preflight(
    *,
    configuration_path: Path = EXCHANGE_CONFIGURATION_ENVIRONMENT,
    secret_path: Path = EXCHANGE_SECRET_ENVIRONMENT,
    authority_configuration_path: Path = AUTHORITY_CONFIGURATION_ENVIRONMENT,
    command: Path = CYRENE_COMMAND,
    runner: Any = subprocess.run,
    owner_uid: int | None = None,
    timeout_seconds: int = 60,
) -> None:
    """Run Exchange's strict local config parser before a Package Runtime plan.

    The active immutable Exchange bundle owns schema validation, certificate
    fingerprint matching, fixed capability bindings, inbound-source checks,
    and secret readiness. This preflight opens no Product listener or database.
    """

    if not hasattr(os, "geteuid"):
        raise ExchangeIntegrationPreflightError(
            "Exchange integration installation requires a Unix host."
        )
    expected_uid = os.geteuid() if owner_uid is None else owner_uid
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/var/lib/cyrene",
        "LANG": "C.UTF-8",
    }
    environment.update(
        _read_environment_file(
            configuration_path,
            required=True,
            secret=False,
            owner_uid=expected_uid,
        )
    )
    if PRODUCT_WORKLOAD_PROFILES_ENVIRONMENT_NAME in environment:
        raise ExchangeIntegrationPreflightError(
            "Platform Product workload profiles must remain in the Platform-owned configuration."
        )
    authority_configuration = _read_environment_file(
        authority_configuration_path,
        required=True,
        secret=False,
        owner_uid=expected_uid,
        selected_names=frozenset({PRODUCT_WORKLOAD_PROFILES_ENVIRONMENT_NAME}),
    )
    profiles_json = authority_configuration.get(PRODUCT_WORKLOAD_PROFILES_ENVIRONMENT_NAME)
    if not profiles_json:
        raise ExchangeIntegrationPreflightError(
            "Platform Product workload profiles are missing from the Platform-owned configuration."
        )
    environment[PRODUCT_WORKLOAD_PROFILES_ENVIRONMENT_NAME] = profiles_json
    secret_environment = _read_environment_file(
        secret_path,
        required=False,
        secret=True,
        owner_uid=expected_uid,
    )
    if set(environment).intersection(secret_environment):
        raise ExchangeIntegrationPreflightError(
            "Host Exchange secret environment repeats a configuration variable."
        )
    environment.update(secret_environment)
    environment["CYRENE_EXCHANGE_PROCESS_ROLE"] = "check-config"

    try:
        completed = runner(
            [str(command), "service-run", "exchange"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        raise ExchangeIntegrationPreflightError(
            "The active Exchange release could not validate the integration profile."
        ) from None
    if completed.returncode != 0:
        raise ExchangeIntegrationPreflightError(
            "Exchange integration config, managed Platform profiles, or secret references are not ready. "
            "No Package Runtime plan was written."
        )
