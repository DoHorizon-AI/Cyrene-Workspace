"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 preflight.py                                                         │
│  Module: tooling.deploy.teams_cloud.preflight                           │
│  Role: Perform read-only ARM64 host, Compose, image, and secret checks.  │
│                                                                          │
│  模块职责：只读检查 ARM64 主机、Compose、镜像、密钥和入口。             │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from build_arm64 import BuildError, check_local_lock

GIB = 1024**3
MIB = 1024**2
# Keep preflight and targeted `up` on the same dependency graph; `--no-deps` starts only its target.
# 中文：预检与定向更新使用同一依赖图；`--no-deps` 仍只启动指定目标。
ACTIVE_COMPOSE_PROFILES = ("public-ingress", "cloud-phase2", "runpod", "directory-operator")
# A nominal 4 GiB VM reports less MemTotal after firmware/kernel reservations.
# 中文：4 GiB 规格按 guest 可见下限识别；实际可用内存仍由独立余量检查决定。
MIN_GUEST_MEMORY_MIB = 3584
SECRET_SIZE_MINIMUMS = {
    "authority-signing-seed": 32,
    "authority-tls-key": 1,
    "exchange-authority-client-key": 1,
    "navigator-exchange-hmac-key": 32,
    "teams-navigator-hmac-key": 32,
    "teams-delivery-admin-token": 32,
    "account-center-encryption-key": 32,
    "account-center-exchange-hmac-key": 32,
    "cloud-management-exchange-hmac-key": 32,
    "github-issues-webhook-secret": 32,
    "teams-private-bridge-token": 32,
    "work-actor-token-01": 32,
    "work-actor-token-02": 32,
    "work-actor-token-03": 32,
    "work-actor-token-04": 32,
    "work-actor-token-05": 32,
}
RAW32_SECRET_NAMES = {"authority-signing-seed", "navigator-exchange-hmac-key"}
HEX32_SECRET_NAMES = {
    "account-center-exchange-hmac-key",
    "account-center-github-provider-hmac-key",
    "account-center-hf-provider-hmac-key",
    "cloud-management-exchange-hmac-key",
    "exchange-hf-auth-context-hmac-key",
    "account-center-graph-provider-hmac-key",
    "account-center-ado-provider-hmac-key",
    "exchange-graph-auth-context-hmac-key",
    "exchange-ado-auth-context-hmac-key",
}
HUGGINGFACE_OPTIONAL_SECRET_NAMES = {
    "account-center-hf-provider-hmac-key",
    "account-center-huggingface-oauth-source",
    "exchange-hf-auth-context-hmac-key",
    "huggingface-server-cert",
    "huggingface-server-key",
    "huggingface-health-client-cert",
    "huggingface-health-client-key",
    "huggingface-account-center-client-cert",
    "huggingface-account-center-client-key",
}
CONNECTOR_AUTH_READONLY_PHASES = {"provider-bootstrap", "issued-connector-activation"}
CONNECTOR_AUTH_READONLY_PROVIDERS = {"huggingface", "azure", "gcp", "runpod"}
CONNECTOR_AUTH_READONLY_OPERATIONS = {
    "huggingface": {"huggingface.whoami", "huggingface.repo.get"},
    "azure": {"azure.resources.list"},
    "gcp": {"gcp.compute.instances.list"},
    "runpod": {"runpod.pods.list"},
}
CONNECTOR_AUTH_READONLY_OPERATIONS_BY_PATH = {
    "provider_identity_bootstrap_api": CONNECTOR_AUTH_READONLY_OPERATIONS,
    "exchange_connector_api": {
        "huggingface": {"huggingface.repo.get"},
        "azure": {"cloud.resources.list", "cloud.provider.health.read"},
        "gcp": {"cloud.resources.list", "cloud.provider.health.read"},
        "runpod": {"cloud.resources.list", "cloud.provider.health.read"},
    },
}
CONNECTOR_AUTH_READONLY_AUTH_MODES = {
    "huggingface": {"device_oauth"},
    "azure": {"managed_identity"},
    "gcp": {"workload_identity_federation"},
    "runpod": {"api_key"},
}
CONNECTOR_MODEL_READONLY_OPERATIONS = {
    "huggingface": {
        "huggingface.models.list",
        "huggingface.datasets.list",
        "huggingface.spaces.list",
        "huggingface.repo.get",
        "huggingface.file.download",
    },
    "azure": {
        "cloud.provider.health.read",
        "cloud.resources.list",
        "cloud.resource.get",
        "cloud.operation.status.read",
        "cloud.operation.logs.read",
    },
    "gcp": {
        "cloud.provider.health.read",
        "cloud.resources.list",
        "cloud.resource.get",
        "cloud.operation.status.read",
        "cloud.operation.logs.read",
    },
    "runpod": {
        "cloud.provider.health.read",
        "cloud.resources.list",
        "cloud.resource.get",
        "cloud.operation.status.read",
        "cloud.operation.logs.read",
    },
}
PEM_PRIVATE_KEY_NAMES = {
    "authority-tls-key",
    "exchange-authority-client-key",
    "exchange-tls-key",
    "navigator-persistence-tls-key",
    "exchange-plugin-client-key",
    "exchange-account-center-client-key",
    "account-center-server-key",
    "account-center-health-client-key",
    "github-issues-server-key",
    "github-issues-health-client-key",
    "github-issues-account-center-client-key",
    "huggingface-server-key",
    "huggingface-health-client-key",
    "huggingface-account-center-client-key",
    "teams-connector-server-key",
    "teams-connector-health-client-key",
    "cloud-management-server-key",
    "cloud-management-health-client-key",
    "computer-runtime-proxy-server-key",
    "account-center-github-app-private-key",
    "microsoft-graph-server-key",
    "microsoft-graph-health-client-key",
    "microsoft-graph-account-center-client-key",
    "azure-devops-observation-server-key",
    "azure-devops-observation-health-client-key",
    "azure-devops-observation-account-center-client-key",
}
PEM_CERTIFICATE_NAMES = {
    "postgres-server-cert",
    "postgres-root-ca",
    "authority-tls-cert",
    "authority-tls-client-ca",
    "internal-service-root-ca",
    "exchange-tls-cert",
    "navigator-persistence-tls-cert",
    "exchange-authority-root-ca",
    "exchange-authority-client-cert",
    "exchange-account-center-client-cert",
    "exchange-plugin-root-ca",
    "exchange-plugin-client-ca",
    "account-center-server-cert",
    "account-center-client-ca",
    "account-center-health-client-cert",
    "github-issues-server-cert",
    "github-issues-health-client-cert",
    "github-issues-account-center-client-cert",
    "huggingface-server-cert",
    "huggingface-health-client-cert",
    "huggingface-account-center-client-cert",
    "teams-connector-server-cert",
    "teams-connector-health-client-cert",
    "cloud-management-server-cert",
    "cloud-management-health-client-cert",
    "computer-runtime-proxy-server-cert",
    "microsoft-graph-server-cert",
    "microsoft-graph-health-client-cert",
    "microsoft-graph-account-center-client-cert",
    "azure-devops-observation-server-cert",
    "azure-devops-observation-health-client-cert",
    "azure-devops-observation-account-center-client-cert",
}
SECRET_CONTENT_VALIDATED_NAMES = (
    RAW32_SECRET_NAMES
    | HEX32_SECRET_NAMES
    | PEM_PRIVATE_KEY_NAMES
    | PEM_CERTIFICATE_NAMES
    | {"teams-navigator-hmac-key", "account-center-encryption-key"}
)
PLACEHOLDER_RE = re.compile(
    r"(replace[_ -]?with|example\.invalid|changeme|placeholder|<[^>]+>)", re.IGNORECASE
)
SECRET_NAME_RE = re.compile(r"(secret|token|password|private.?key|api.?key)", re.IGNORECASE)
AUTOMATION_WORKFLOW_TOKEN_SLOTS = tuple(
    (f"CYRENE_WORK_ACTOR_TOKEN_{index:02d}", f"work-actor-token-{index:02d}")
    for index in range(1, 6)
)
AUTOMATION_WORKFLOW_SECRET_NAMES = {
    "teams-private-bridge-token",
    *(secret for _environment, secret in AUTOMATION_WORKFLOW_TOKEN_SLOTS),
}
AUTOMATION_HEALTH_TARGET = "/run/cyrene/automation-health"
AUTOMATION_PROVIDER_SERVICE_NAMES = {
    "microsoft-graph",
    "azure-devops-observation",
}


class PreflightError(ValueError):
    """A host, workload, or external-identity condition failed closed."""


def _private_artifact(path: Path) -> bool:
    """Require private configuration and receipts outside source checkouts."""

    resolved = path.resolve()
    return ".artifacts" in resolved.parts and not any(
        (parent / ".git").exists() for parent in (resolved, *resolved.parents)
    )


def _run(argv: list[str], *, capture: bool = True) -> str:
    """Run one read-only command and suppress provider/host output details."""

    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True)
    except OSError as error:
        raise PreflightError(f"required executable is unavailable: {Path(argv[0]).name}") from error
    if result.returncode != 0:
        raise PreflightError(f"read-only preflight command failed: {Path(argv[0]).name}")
    return result.stdout if capture else ""


def _parse_meminfo() -> int:
    """Return host memory in MiB from procfs."""

    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError) as error:
        raise PreflightError("host memory could not be read") from error
    raise PreflightError("host memory could not be read")


def _parse_memavailable() -> int:
    """Return current kernel-estimated memory available without swapping, in MiB."""

    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError) as error:
        raise PreflightError("host available memory could not be read") from error
    raise PreflightError("host available memory could not be read")


def _secret_file(path: Path, expected_name: str) -> None:
    """Validate a mounted host credential without printing or copying contents."""

    if path.is_symlink() or not path.is_file():
        raise PreflightError(f"required secret file is missing or unsafe: {expected_name}")
    metadata = path.stat()
    if metadata.st_mode & 0o077:
        raise PreflightError(f"secret file permissions are too broad: {expected_name}")
    if expected_name == "teams-delivery-admin-token" and stat.S_IMODE(metadata.st_mode) != 0o400:
        raise PreflightError("Teams delivery admin token must use owner-read-only mode 0400")
    if not metadata.st_mode & 0o400:
        raise PreflightError(f"secret file is not readable by its container owner: {expected_name}")
    expected_owner = (
        999
        if expected_name in {"postgres-server-cert", "postgres-server-key"}
        else 0
        if expected_name == "postgres-admin-password"
        else 10001
    )
    if metadata.st_uid != expected_owner:
        raise PreflightError(f"secret file owner UID is wrong for its container: {expected_name}")
    minimum = SECRET_SIZE_MINIMUMS.get(expected_name, 1)
    if expected_name == "authority-signing-seed" and metadata.st_size != 32:
        raise PreflightError("Authority signing seed must contain exactly 32 raw bytes")
    if metadata.st_size < minimum:
        raise PreflightError(f"secret file is too short: {expected_name}")
    if expected_name not in SECRET_CONTENT_VALIDATED_NAMES:
        return
    value = path.read_bytes()
    if expected_name in RAW32_SECRET_NAMES and len(value) != 32:
        raise PreflightError(f"raw HMAC/signing key must contain exactly 32 bytes: {expected_name}")
    if expected_name in HEX32_SECRET_NAMES and not re.fullmatch(rb"[0-9a-fA-F]{64}", value):
        raise PreflightError(
            f"HMAC key must contain exactly 64 hex characters without a newline: {expected_name}"
        )
    if expected_name == "teams-navigator-hmac-key" and not re.fullmatch(rb"[0-9a-fA-F]{64}", value):
        raise PreflightError("Teams-Navigator key must be ASCII hex64 without a trailing newline")
    if expected_name == "account-center-encryption-key" and not re.fullmatch(
        rb"[0-9a-fA-F]{64}", value
    ):
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as error:
            raise PreflightError(
                "Account Center encryption key must be canonical standard base64 or 64 hex characters"
            ) from error
        if len(decoded) != 32 or base64.b64encode(decoded) != value:
            raise PreflightError("Account Center encryption key must encode exactly 32 bytes")
    if expected_name in PEM_PRIVATE_KEY_NAMES and not (
        value.startswith(b"-----BEGIN ") and b"PRIVATE KEY-----" in value[:128]
    ):
        raise PreflightError(f"private key file must contain a PEM private key: {expected_name}")
    if expected_name in PEM_CERTIFICATE_NAMES and b"-----BEGIN CERTIFICATE-----" not in value:
        raise PreflightError(f"certificate file must contain PEM certificate data: {expected_name}")


def _validate_directory_database_urls(secrets_dir: Path) -> None:
    """Check role-specific PostgreSQL URLs and their TLS scope without reporting values."""

    expected_users = {
        "directory-admin-database-url": "cyrene_directory_admin",
        "directory-operator-database-url": "cyrene_directory_operator",
        "directory-runtime-reader-database-url": "cyrene_authority_app",
    }
    passwords: list[str] = []
    parsed: dict[str, dict[str, Any]] = {}
    for filename, expected_user in expected_users.items():
        try:
            value = (secrets_dir / filename).read_text(encoding="utf-8").strip()
            uri = urlsplit(value)
            port = uri.port
            query = parse_qs(uri.query, strict_parsing=True)
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise PreflightError(f"Directory database URL is invalid: {filename}") from error
        password = unquote(uri.password or "")
        if (
            uri.scheme not in {"postgres", "postgresql"}
            or uri.hostname != "postgres"
            or port not in {None, 5432}
            or uri.username != expected_user
            or uri.path != "/cyrene_directory"
            or query.get("sslmode") != ["verify-full"]
            or query.get("sslrootcert") != ["/run/secrets/postgres-root-ca"]
            or not password
        ):
            raise PreflightError(
                f"Directory database URL does not match the isolated role/TLS target: {filename}"
            )
        passwords.append(password)
        parsed[filename] = {"user": uri.username, "password": password}
    if len(set(passwords)) != 3:
        raise PreflightError(
            "Directory admin, operator, and runtime reader logins must have distinct passwords"
        )
    try:
        postgres_password = (
            (secrets_dir / "postgres-admin-password").read_text(encoding="utf-8").rstrip("\r\n")
        )
    except (OSError, UnicodeDecodeError) as error:
        raise PreflightError("Postgres initialization password is unavailable") from error
    if postgres_password != parsed["directory-admin-database-url"]["password"]:
        raise PreflightError(
            "Postgres initialization password does not match the one-shot Directory admin URL"
        )


def _validate_huggingface_oauth_source(path: Path) -> None:
    """Check a private device-OAuth bootstrap bundle without displaying token data."""

    if path.is_symlink() or not path.is_file():
        raise PreflightError("Hugging Face OAuth bootstrap file is missing or unsafe")
    if path.stat().st_size > 1_000_000:
        raise PreflightError("Hugging Face OAuth bootstrap file exceeds the allowed size")
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError("Hugging Face OAuth bootstrap file is invalid JSON") from error
    fields = {
        "schema_version",
        "provider",
        "auth_mode",
        "client_id",
        "oauth_scopes",
        "access_token",
        "refresh_token",
        "access_token_expires_at",
        "refresh_token_expires_at",
        "target_scope",
        "credential_secret_ref",
    }
    scope = document.get("target_scope") if isinstance(document, dict) else None
    repositories = scope.get("repositories") if isinstance(scope, dict) else None
    oauth_scopes = document.get("oauth_scopes") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 1
        or isinstance(document.get("schema_version"), bool)
        or document.get("provider") != "huggingface"
        or document.get("auth_mode") != "device_oauth"
        or document.get("credential_secret_ref") != "huggingface/connector-token"
        or not _safe_huggingface_oauth_text(document.get("client_id"), maximum=2048)
        or any(character.isspace() for character in document.get("client_id", ""))
        or not _safe_huggingface_oauth_text(document.get("access_token"), maximum=8192)
        or not _safe_huggingface_oauth_text(document.get("refresh_token"), maximum=8192)
        or not isinstance(scope, dict)
        or set(scope) != {"repositories"}
        or (
            oauth_scopes is not None
            and (
                not isinstance(oauth_scopes, list)
                or any(not isinstance(value, str) for value in oauth_scopes)
                or len(oauth_scopes) != len(set(oauth_scopes))
                or set(oauth_scopes)
                - {"openid", "profile", "read-repos", "gated-repos", "write-repos"}
            )
        )
        or not _valid_positive_timestamp(document.get("access_token_expires_at"))
        or (
            document.get("refresh_token_expires_at") is not None
            and not _valid_positive_timestamp(document.get("refresh_token_expires_at"))
        )
        or not isinstance(repositories, list)
        or not _valid_huggingface_repository_scope(repositories)
    ):
        raise PreflightError("Hugging Face OAuth bootstrap fields are incomplete or out of scope")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous duplicate keys in private configuration JSON."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _safe_huggingface_oauth_text(value: Any, *, maximum: int) -> bool:
    """Match the Account Center's bounded, non-control-text checks."""

    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and "\r" not in value
        and "\n" not in value
        and "\x00" not in value
        and value.isprintable()
    )


def _valid_positive_timestamp(value: Any) -> bool:
    """Reject booleans and nonpositive OAuth expiry values."""

    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_huggingface_repository_scope(repositories: Any) -> bool:
    """Require the same closed repository permission shape used by Account Center."""

    if not isinstance(repositories, list) or not 1 <= len(repositories) <= 100:
        return False
    seen: set[tuple[str, str]] = set()
    for repository in repositories:
        if not isinstance(repository, dict) or set(repository) != {
            "repo_type",
            "repo_id",
            "permissions",
        }:
            return False
        repo_type = repository["repo_type"]
        repo_id = repository["repo_id"]
        permissions = repository["permissions"]
        if (
            not isinstance(repo_type, str)
            or repo_type not in {"model", "dataset", "space"}
            or not isinstance(repo_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo_id)
            or not isinstance(permissions, list)
            or not permissions
            or any(not isinstance(permission, str) for permission in permissions)
            or set(permissions) - {"read", "write"}
            or len(permissions) != len(set(permissions))
            or (repo_type, repo_id) in seen
        ):
            return False
        seen.add((repo_type, repo_id))
    return True


def _validate_keyring(path: Path, expected_key_id: str | None = None) -> None:
    """Require a real Exchange keyring with only pinned raw Ed25519 public keys."""

    if path.is_symlink() or not path.is_file():
        raise PreflightError("Authority public keyring file is unavailable")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreflightError("Authority public keyring is invalid JSON") from error
    if (
        not isinstance(document, dict)
        or set(document) != {"keys"}
        or not isinstance(document["keys"], dict)
        or not document["keys"]
    ):
        raise PreflightError("Authority public keyring must contain one or more key IDs")
    if expected_key_id and expected_key_id not in document["keys"]:
        raise PreflightError(
            "Authority public keyring does not contain the configured active signing key ID"
        )
    for key_id, encoded in document["keys"].items():
        if PLACEHOLDER_RE.search(key_id) or not isinstance(encoded, str):
            raise PreflightError("Authority public keyring contains a placeholder")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise PreflightError("Authority public keyring contains malformed key data") from error
        if len(raw) != 32:
            raise PreflightError("Authority public keyring keys must be raw Ed25519 public keys")


def _automation_workflow_markers_present(services: Any, configured_secrets: Any = None) -> bool:
    """Detect any part of the optional private-workflow overlay, including partial overlays."""

    if not isinstance(services, dict):
        return False
    if (
        isinstance(configured_secrets, dict)
        and set(configured_secrets) & AUTOMATION_WORKFLOW_SECRET_NAMES
    ):
        return True
    navigator = services.get("navigator")
    persistence = services.get("navigator-persistence")
    navigator_environment = navigator.get("environment", {}) if isinstance(navigator, dict) else {}
    persistence_environment = (
        persistence.get("environment", {}) if isinstance(persistence, dict) else {}
    )
    marker_names = {
        "CYRENE_TEAMS_PERSONAL_CHAT_ENABLED",
        "CYRENE_WORK_ACTOR_TOKEN_MAP_FILE",
        "CYRENE_AUTOMATION_SOURCE_BINDINGS_FILE",
    }
    if isinstance(navigator_environment, dict) and marker_names.intersection(navigator_environment):
        return True
    for environment in (navigator_environment, persistence_environment):
        encoded = (
            environment.get("CYRENE_SECRET_FILE_MAP", "") if isinstance(environment, dict) else ""
        )
        if isinstance(encoded, str) and any(
            marker in encoded
            for marker in (
                "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN",
                "CYRENE_WORK_ACTOR_TOKEN_",
                "/run/secrets/teams-private-bridge-token",
            )
        ):
            return True
    for service in services.values():
        if not isinstance(service, dict):
            continue
        if any(
            isinstance(row, dict) and row.get("source") in AUTOMATION_WORKFLOW_SECRET_NAMES
            for row in service.get("secrets", [])
        ):
            return True
        if any(
            isinstance(row, dict) and row.get("target") == AUTOMATION_HEALTH_TARGET
            for row in service.get("volumes", [])
        ):
            return True
    return False


def _uuid(value: Any) -> bool:
    """Accept canonical lowercase UUID strings for private identity references."""

    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value)
        is not None
    )


def _validate_principal_configuration(
    path: Path,
    services: dict[str, Any],
    *,
    connector_auth_phase: str | None = None,
) -> None:
    """Bind the Navigator session principal to the approved Directory scope."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreflightError("Navigator principal configuration is invalid JSON") from error
    navigator_environment = services.get("navigator", {}).get("environment", {})
    operator_environment = services.get("directory-operator", {}).get("environment", {})
    expected_actor = operator_environment.get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID")
    expected_workspace = navigator_environment.get("CYRENE_WORKSPACE_ID")
    expected_organization = operator_environment.get("CYRENE_ORGANIZATION_ID")
    principals = document.get("principals") if isinstance(document, dict) else None
    automation_overlay = _automation_workflow_markers_present(services)
    expected_principal_count = 7 if automation_overlay else 1
    if (
        not isinstance(principals, list)
        or len(principals) != expected_principal_count
        or any(not isinstance(principal, dict) for principal in principals)
    ):
        raise PreflightError(
            "production Navigator principal list does not match its enabled actor profile"
        )
    by_token_env: dict[str, dict[str, Any]] = {}
    for principal in principals:
        token_env = principal.get("token_env")
        if not isinstance(token_env, str) or token_env in by_token_env:
            raise PreflightError("Navigator Work principal token references must be unique")
        by_token_env[token_env] = principal
    session = by_token_env.get("NAVIGATOR_SESSION_TOKEN")
    if (
        session is None
        or session.get("actor_id") != expected_actor
        or session.get("workspace_ids") != [expected_workspace]
        or session.get("organization_id") != expected_organization
        or session.get("can_takeover") is not True
        or session.get("actor_private_scope", False) is not False
        or session.get("can_write_harness", False) is not False
        or session.get("can_bind_teams_private_conversations", False) is not False
    ):
        raise PreflightError(
            "Navigator must preserve the scoped owner session principal and its existing grants"
        )
    if automation_overlay:
        expected_tokens = {
            "NAVIGATOR_SESSION_TOKEN",
            *(environment for environment, _secret in AUTOMATION_WORKFLOW_TOKEN_SLOTS),
            "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN",
        }
        if set(by_token_env) != expected_tokens:
            raise PreflightError(
                "automation Work principals must be the existing owner session, five private writers, and one bridge"
            )
        writer_actor_ids: set[str] = set()
        for index, (token_env, _secret) in enumerate(AUTOMATION_WORKFLOW_TOKEN_SLOTS, start=1):
            principal = by_token_env[token_env]
            actor_id = principal.get("actor_id")
            if (
                not _uuid(actor_id)
                or principal.get("workspace_ids") != [expected_workspace]
                or principal.get("organization_id") != expected_organization
                or principal.get("can_takeover") is not True
                or principal.get("can_write_harness") is not True
                or principal.get("actor_private_scope") is not True
                or principal.get("can_publish_credential_health") is not False
                or principal.get("can_bind_teams_private_conversations") is not False
                or (index == 1 and actor_id != expected_actor)
                or (index > 1 and actor_id == expected_actor)
                or actor_id in writer_actor_ids
            ):
                raise PreflightError(
                    "automation Work writer principals must keep exact actor-private grants and unique actors"
                )
            writer_actor_ids.add(actor_id)
        bridge = by_token_env["CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN"]
        if (
            bridge.get("actor_id") != "teams-private-bridge"
            or bridge.get("workspace_ids") != [expected_workspace]
            or bridge.get("organization_id") != expected_organization
            or bridge.get("can_takeover") is not False
            or bridge.get("can_write_harness") is not False
            or bridge.get("actor_private_scope") is not False
            or bridge.get("can_publish_credential_health") is not False
            or bridge.get("can_bind_teams_private_conversations") is not True
        ):
            raise PreflightError(
                "private Teams bridge must use its non-human identity and only the conversation-bind grant"
            )
    elif len(by_token_env) != 1:
        raise PreflightError(
            "owner-only deployment must retain exactly its original session principal"
        )
    if document.get("product_directory") != [] or document.get("product_service_credentials") != []:
        raise PreflightError("unconfigured Product proxy credentials must remain absent")
    if connector_auth_phase == "provider-bootstrap":
        if session.get("can_publish_credential_health") not in {None, False}:
            raise PreflightError(
                "credential notification publishing must remain disabled during provider bootstrap"
            )
    elif (
        connector_auth_phase == "issued-connector-activation"
        and session.get("can_publish_credential_health") is not True
    ):
        raise PreflightError(
            "credential notification activation requires the owner publishing role"
        )


def _validate_actor_projection(path: Path, services: dict[str, Any]) -> None:
    """Require the private dynamic Teams allowlist to match the tenant and Workspace."""

    if path.is_symlink() or not path.is_file():
        raise PreflightError("dynamic Teams actor-subject projection is missing")
    metadata = path.stat()
    if metadata.st_mode & 0o077 or metadata.st_uid != 10001 or not metadata.st_mode & 0o400:
        raise PreflightError(
            "dynamic Teams actor-subject file must be owner-only and owned by UID 10001"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreflightError("dynamic Teams actor-subject projection is invalid JSON") from error
    navigator_environment = services.get("navigator", {}).get("environment", {})
    operator_environment = services.get("directory-operator", {}).get("environment", {})
    tenant = navigator_environment.get("TEAMS_TENANT_ID")
    workspace = navigator_environment.get("CYRENE_WORKSPACE_ID")
    actor = operator_environment.get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID")
    expected_issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
    if (
        not isinstance(document, dict)
        or set(document) != {"schemaVersion", "workspaceId", "issuer", "subjects"}
        or document.get("schemaVersion") != 1
        or document.get("workspaceId") != workspace
        or document.get("issuer") != expected_issuer
        or not isinstance(document.get("subjects"), list)
        or not document["subjects"]
        or actor not in document["subjects"]
        or any(
            not isinstance(subject, str)
            or not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", subject
            )
            for subject in document["subjects"]
        )
        or len(set(document["subjects"])) != len(document["subjects"])
    ):
        raise PreflightError(
            "dynamic Teams actor-subject file does not match the approved tenant/workspace owner scope"
        )


def _private_runtime_json(path: Path, name: str, *, maximum_bytes: int = 1_000_000) -> Any:
    """Read one small owner-only runtime JSON file while rejecting aliases and duplicate keys."""

    try:
        metadata = path.lstat()
    except OSError as error:
        raise PreflightError(f"private automation configuration is unavailable: {name}") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 10001
        or metadata.st_mode & 0o077
        or not metadata.st_mode & 0o400
        or metadata.st_size < 1
        or metadata.st_size > maximum_bytes
    ):
        raise PreflightError(f"private automation configuration metadata is unsafe: {name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError(f"private automation configuration is invalid JSON: {name}") from error


def _parse_secret_file_map(service: Any, label: str) -> dict[str, str]:
    """Parse the launcher secret map with unique names and canonical container paths."""

    environment = service.get("environment", {}) if isinstance(service, dict) else {}
    encoded = environment.get("CYRENE_SECRET_FILE_MAP") if isinstance(environment, dict) else None
    if not isinstance(encoded, str) or not encoded:
        raise PreflightError(f"{label} must declare its exact secret-file map")
    result: dict[str, str] = {}
    for part in encoded.split(","):
        if part.count("=") != 1:
            raise PreflightError(f"{label} secret-file map is malformed")
        name, path = part.split("=", 1)
        if (
            not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name)
            or not re.fullmatch(r"/run/secrets/[A-Za-z0-9][A-Za-z0-9._-]{0,127}", path)
            or name in result
        ):
            raise PreflightError(f"{label} secret-file map has duplicate or unsafe entries")
        result[name] = path
    return result


def _secret_mount_pairs(service: Any, label: str) -> set[tuple[str, str]]:
    """Return the exact source/target pairs attached to one Compose service."""

    rows = service.get("secrets", []) if isinstance(service, dict) else []
    if not isinstance(rows, list):
        raise PreflightError(f"{label} secret mounts are malformed")
    pairs: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PreflightError(f"{label} secret mounts are malformed")
        source = row.get("source")
        target = row.get("target", source)
        if not isinstance(source, str) or not isinstance(target, str) or (source, target) in pairs:
            raise PreflightError(f"{label} secret mounts contain duplicates or invalid names")
        pairs.add((source, target))
    return pairs


def _validate_automation_source_bindings(
    document: Any,
    *,
    actor_ids: set[str],
    connector_bindings: Any,
    organization_id: str,
    workspace_id: str,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Validate fixed source selectors; do not infer or grant upstream access."""

    if (
        not isinstance(document, dict)
        or set(document) != {"schemaVersion", "targets"}
        or document.get("schemaVersion") != 1
        or not isinstance(document.get("targets"), list)
        or len(document["targets"]) > 100
    ):
        raise PreflightError("automation source bindings have an unsupported schema")
    seen_bindings: set[str] = set()
    seen_targets: set[tuple[str, str]] = set()
    targets: list[dict[str, Any]] = []
    for row in document["targets"]:
        if not isinstance(row, dict):
            raise PreflightError("automation source binding is malformed")
        kind = row.get("kind")
        if not isinstance(kind, str):
            raise PreflightError("automation source binding kind is invalid")
        expected_fields = {
            "training_run": {
                "actorId",
                "targetId",
                "kind",
                "bindingId",
                "product",
                "resourceKind",
                "resourceId",
                "organizationId",
                "workspaceId",
            },
            "product_resource": {
                "actorId",
                "targetId",
                "kind",
                "bindingId",
                "product",
                "resourceKind",
                "resourceId",
                "organizationId",
                "workspaceId",
            },
            "host_health": {
                "actorId",
                "targetId",
                "kind",
                "bindingId",
                "hostRef",
                "mountDirectory",
                "mountGid",
                "maxAgeSeconds",
                "maxClockSkewSeconds",
            },
            "planner_plan": {
                "actorId",
                "targetId",
                "kind",
                "bindingId",
                "connectionRef",
                "profileRef",
                "tenantId",
                "planId",
            },
        }.get(kind)
        if expected_fields is None or set(row) != expected_fields:
            raise PreflightError("automation source binding contains an unsupported field or kind")
        actor_id = row.get("actorId")
        target_id = row.get("targetId")
        binding_id = row.get("bindingId")
        if (
            not _uuid(actor_id)
            or actor_id not in actor_ids
            or not isinstance(target_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}", target_id)
            or not _uuid(binding_id)
            or binding_id in seen_bindings
            or (actor_id, target_id) in seen_targets
        ):
            raise PreflightError("automation source binding identity or lookup alias is invalid")
        seen_bindings.add(binding_id)
        seen_targets.add((actor_id, target_id))
        if kind in {"training_run", "product_resource"}:
            if (
                not _uuid(row.get("resourceId"))
                or row.get("organizationId") != organization_id
                or row.get("workspaceId") != workspace_id
            ):
                raise PreflightError("Product source binding does not match its scoped Workspace")
            if kind == "training_run" and (
                row.get("product") != "yield" or row.get("resourceKind") != "training-run"
            ):
                raise PreflightError("training source binding must select a Yield training run")
            if kind == "product_resource" and (
                not isinstance(row.get("product"), str)
                or not isinstance(row.get("resourceKind"), str)
                or (row.get("product"), row.get("resourceKind"))
                not in {
                    ("catalyst", "dataset-version"),
                    ("echo", "evaluation-run"),
                    ("reactor", "deployment"),
                }
            ):
                raise PreflightError("Product source binding does not match a fixed resource type")
        elif kind == "host_health":
            if (
                not isinstance(row.get("hostRef"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", row["hostRef"])
                or row.get("mountDirectory") != AUTOMATION_HEALTH_TARGET
                or type(row.get("mountGid")) is not int
                or not 0 <= row["mountGid"] <= 2_147_483_647
                or type(row.get("maxAgeSeconds")) is not int
                or not 1 <= row["maxAgeSeconds"] <= 600
                or type(row.get("maxClockSkewSeconds")) is not int
                or not 0 <= row["maxClockSkewSeconds"] <= 120
            ):
                raise PreflightError(
                    "host-health binding exceeds the sanitized observation boundary"
                )
        else:
            for field in ("connectionRef", "profileRef", "planId"):
                value = row.get(field)
                if not isinstance(value, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}", value
                ):
                    raise PreflightError("Planner binding contains an unsafe lookup reference")
            if row.get("tenantId") != tenant_id:
                raise PreflightError("Planner source binding must stay within the verified tenant")
            _require_planner_operations_for_binding(row, connector_bindings)
        targets.append(row)
    return targets


def _require_planner_operations_for_binding(
    source: dict[str, Any], connector_bindings: Any
) -> None:
    """Bind a Planner source to one actor/profile connection exposing both required reads."""

    if (
        not isinstance(connector_bindings, dict)
        or set(connector_bindings) != {"schemaVersion", "bindings"}
        or connector_bindings.get("schemaVersion") != 1
        or not isinstance(connector_bindings.get("bindings"), list)
    ):
        raise PreflightError("Planner source requires a validated connector binding document")
    matching = [
        binding
        for binding in connector_bindings["bindings"]
        if isinstance(binding, dict)
        and binding.get("providerId") == "microsoft_graph"
        and binding.get("connectionRef") == source.get("connectionRef")
        and binding.get("profileRef") == source.get("profileRef")
    ]
    if len(matching) != 1:
        raise PreflightError(
            "Planner source must resolve to one matching actor-bound Graph connection"
        )
    tools = matching[0].get("tools")
    if not isinstance(tools, list):
        raise PreflightError("Planner Graph binding has no fixed read-operation map")
    operations = {tool.get("operationId"): tool for tool in tools if isinstance(tool, dict)}
    for operation_id in (
        "microsoft_graph.planner.plan.get",
        "microsoft_graph.planner.tasks.list",
    ):
        tool = operations.get(operation_id)
        if (
            not isinstance(tool, dict)
            or tool.get("toolId") != operation_id
            or tool.get("resourceProjection") != "microsoft_graph_plan"
            or tool.get("mutating") is not False
        ):
            raise PreflightError(
                "Planner source requires both matching plan metadata and task-list read operations"
            )


def _validate_automation_workflow_configuration(
    config: dict[str, Any], *, config_dir: Path, secrets_dir: Path
) -> bool:
    """Validate the optional actor-scoped preparation profile without claiming live acceptance."""

    services = config.get("services", {})
    if not _automation_workflow_markers_present(services, config.get("secrets")):
        return False
    navigator = services.get("navigator")
    persistence = services.get("navigator-persistence")
    if not isinstance(navigator, dict) or not isinstance(persistence, dict):
        raise PreflightError("automation workflow overlay requires Navigator and Persistence")
    navigator_environment = navigator.get("environment", {})
    expected_navigator_environment = {
        "CYRENE_WORK_ACTOR_TOKEN_MAP_FILE": "/run/teams-authorization/work-actor-token-map.json",
        "CYRENE_AUTOMATION_SOURCE_BINDINGS_FILE": "/run/teams-authorization/automation-source-bindings.json",
    }
    if (
        not isinstance(navigator_environment, dict)
        or any(
            navigator_environment.get(key) != value
            for key, value in expected_navigator_environment.items()
        )
        or navigator_environment.get("CYRENE_WORKFLOWS_ENABLED") not in {"false", "true"}
        or navigator_environment.get("CYRENE_TEAMS_PERSONAL_CHAT_ENABLED") not in {"false", "true"}
    ):
        raise PreflightError(
            "automation workflow environment must use its exact private config paths"
        )

    expected_navigator_secret_map = {
        "CYRENE_SESSION_TOKEN": "/run/secrets/navigator-session-token",
        "TEAMS_NAVIGATOR_HMAC_KEY": "/run/secrets/teams-navigator-hmac-key",
        "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN": "/run/secrets/teams-private-bridge-token",
        **{
            environment: f"/run/secrets/{secret}"
            for environment, secret in AUTOMATION_WORKFLOW_TOKEN_SLOTS
        },
    }
    expected_persistence_secret_map = {
        "NAVIGATOR_SESSION_TOKEN": "/run/secrets/navigator-session-token",
        "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN": "/run/secrets/teams-private-bridge-token",
        **{
            environment: f"/run/secrets/{secret}"
            for environment, secret in AUTOMATION_WORKFLOW_TOKEN_SLOTS
        },
    }
    if _parse_secret_file_map(navigator, "Navigator") != expected_navigator_secret_map:
        raise PreflightError(
            "Navigator actor tokens must remain separate from the existing session token"
        )
    if (
        _parse_secret_file_map(persistence, "Navigator Persistence")
        != expected_persistence_secret_map
    ):
        raise PreflightError("Persistence actor tokens must use their dedicated secret slots")
    authorization_mounts = [
        row
        for row in navigator.get("volumes", [])
        if isinstance(row, dict) and row.get("target") == "/run/teams-authorization"
    ]
    if (
        len(authorization_mounts) != 1
        or authorization_mounts[0].get("type") != "bind"
        or authorization_mounts[0].get("read_only") is not True
        or not isinstance(authorization_mounts[0].get("source"), str)
        or Path(authorization_mounts[0]["source"]).resolve() != config_dir.resolve()
    ):
        raise PreflightError(
            "automation identity and source maps require the existing read-only config mount"
        )
    base_navigator_secret_pairs = {
        ("navigator-exchange-hmac-key", "navigator-exchange-hmac-key"),
        ("navigator-session-token", "navigator-session-token"),
        ("teams-navigator-hmac-key", "teams-navigator-hmac-key"),
        ("internal-service-root-ca", "internal-service-root-ca"),
    }
    base_persistence_secret_pairs = {
        ("navigator-session-token", "navigator-session-token"),
        ("navigator-persistence-tls-cert", "navigator-persistence-tls-cert"),
        ("navigator-persistence-tls-key", "navigator-persistence-tls-key"),
        ("internal-service-root-ca", "internal-service-root-ca"),
    }
    automation_pairs = {(name, name) for name in AUTOMATION_WORKFLOW_SECRET_NAMES}
    if (
        _secret_mount_pairs(navigator, "Navigator")
        != base_navigator_secret_pairs | automation_pairs
        or _secret_mount_pairs(persistence, "Navigator Persistence")
        != base_persistence_secret_pairs | automation_pairs
    ):
        raise PreflightError("automation secrets must mount only to Navigator and Persistence")
    consumers: dict[str, set[str]] = {name: set() for name in AUTOMATION_WORKFLOW_SECRET_NAMES}
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        for row in service.get("secrets", []):
            if isinstance(row, dict) and row.get("source") in consumers:
                consumers[row["source"]].add(service_name)
    if any(value != {"navigator", "navigator-persistence"} for value in consumers.values()):
        raise PreflightError(
            "automation actor and bridge secrets have an unexpected service consumer"
        )
    for secret_name in AUTOMATION_WORKFLOW_SECRET_NAMES:
        secret = config.get("secrets", {}).get(secret_name)
        source = secret.get("file") if isinstance(secret, dict) else None
        if (
            not isinstance(source, str)
            or Path(source).resolve() != (secrets_dir / secret_name).resolve()
        ):
            raise PreflightError(
                "automation Work secrets must resolve to their dedicated private files"
            )

    actor_map = _private_runtime_json(
        config_dir / "work-actor-token-map.json", "work actor token map"
    )
    actor_projection = _private_runtime_json(
        config_dir / "teams-actor-subjects.json", "Teams actor projection"
    )
    source_bindings = _private_runtime_json(
        config_dir / "automation-source-bindings.json", "automation source bindings"
    )
    connector_bindings = _private_runtime_json(
        config_dir / "connector-bindings.json", "connector bindings"
    )
    environment = navigator_environment
    operator_environment = services.get("directory-operator", {}).get("environment", {})
    owner_id = operator_environment.get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID")
    organization_id = operator_environment.get("CYRENE_ORGANIZATION_ID")
    workspace_id = environment.get("CYRENE_WORKSPACE_ID")
    tenant_id = environment.get("TEAMS_TENANT_ID")
    if (
        not isinstance(actor_map, dict)
        or set(actor_map) != {"schemaVersion", "workspaceId", "actors"}
        or actor_map.get("schemaVersion") != 1
        or actor_map.get("workspaceId") != workspace_id
        or not isinstance(actor_map.get("actors"), list)
        or len(actor_map["actors"]) != 5
    ):
        raise PreflightError(
            "automation actor map must define exactly five private writer identities"
        )
    actor_by_token: dict[str, str] = {}
    for row in actor_map["actors"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"actorAadObjectId", "tokenEnv"}
            or not _uuid(row.get("actorAadObjectId"))
            or row.get("tokenEnv")
            not in {name for name, _secret in AUTOMATION_WORKFLOW_TOKEN_SLOTS}
            or row.get("tokenEnv") in actor_by_token
            or row.get("actorAadObjectId") in actor_by_token.values()
        ):
            raise PreflightError(
                "automation actor map contains a duplicate or unsafe identity reference"
            )
        actor_by_token[row["tokenEnv"]] = row["actorAadObjectId"]
    expected_actor_tokens = {name for name, _secret in AUTOMATION_WORKFLOW_TOKEN_SLOTS}
    if (
        set(actor_by_token) != expected_actor_tokens
        or actor_by_token.get("CYRENE_WORK_ACTOR_TOKEN_01") != owner_id
    ):
        raise PreflightError(
            "automation actor map must bind slot 01 to the owner and four pilot actors"
        )
    model_actor = environment.get("CYRENE_TEAMS_MODEL_ACTOR_SUBJECT", "")
    if not isinstance(model_actor, str) or model_actor not in ("", owner_id):
        raise PreflightError("the existing model authorization may select only the approved owner")
    projection_subjects = (
        actor_projection.get("subjects") if isinstance(actor_projection, dict) else None
    )
    if (
        not isinstance(projection_subjects, list)
        or not 1 <= len(projection_subjects) <= 5
        or any(not _uuid(subject) for subject in projection_subjects)
        or len(set(projection_subjects)) != len(projection_subjects)
        or owner_id not in projection_subjects
        or not set(projection_subjects).issubset(actor_by_token.values())
        or actor_projection.get("workspaceId") != workspace_id
        or actor_projection.get("issuer") != f"https://login.microsoftonline.com/{tenant_id}/v2.0"
    ):
        raise PreflightError(
            "the current Directory projection must remain within configured pilot actors"
        )

    principal_document = _private_runtime_json(
        config_dir / "navigator-principal-config.json", "Navigator principal configuration"
    )
    principals = (
        principal_document.get("principals") if isinstance(principal_document, dict) else None
    )
    if not isinstance(principals, list):
        raise PreflightError("Navigator Work principal configuration is unavailable")
    principals_by_token = {row.get("token_env"): row for row in principals if isinstance(row, dict)}
    for token_env, actor_id in actor_by_token.items():
        principal = principals_by_token.get(token_env)
        if not isinstance(principal, dict) or principal.get("actor_id") != actor_id:
            raise PreflightError(
                "actor token map does not match its immutable Work principal identity"
            )
    if (
        not isinstance(owner_id, str)
        or not isinstance(organization_id, str)
        or not isinstance(workspace_id, str)
        or not isinstance(tenant_id, str)
    ):
        raise PreflightError("automation pilot identity scope is incomplete")
    _validate_automation_source_bindings(
        source_bindings,
        actor_ids=set(actor_by_token.values()),
        connector_bindings=connector_bindings,
        organization_id=organization_id,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
    )
    _validate_automation_health_mount(config, source_bindings)
    _validate_automation_personal_delivery(
        config,
        config_dir=config_dir,
        actor_ids=set(actor_by_token.values()),
        tenant_id=tenant_id,
        principal_document=principal_document,
    )
    return True


def _validate_automation_personal_delivery(
    config: dict[str, Any],
    *,
    config_dir: Path,
    actor_ids: set[str],
    tenant_id: str,
    principal_document: dict[str, Any],
) -> None:
    """Bind optional personal delivery to the same host-selected actors and bot.

    中文：私聊启用时核对同一 actor、Bot 与当前 Directory 投影配置，不把配置当作安装证明。
    """

    services = config.get("services", {})
    persistence = services.get("navigator-persistence", {})
    projection_target = "/run/teams-authorization/teams-actor-subjects.json"
    mounts = [
        row
        for row in persistence.get("volumes", [])
        if isinstance(row, dict) and row.get("target") == "/run/teams-authorization"
    ]
    if (
        persistence.get("environment", {}).get("TEAMS_TASK_ACTOR_SUBJECTS_FILE")
        != projection_target
        or len(mounts) != 1
        or mounts[0].get("type") != "bind"
        or mounts[0].get("read_only") is not True
        or mounts[0].get("bind") != {"create_host_path": False}
        or Path(str(mounts[0].get("source", ""))).resolve() != config_dir.resolve()
    ):
        raise PreflightError("private Work policy requires the current read-only actor projection")
    environment = services.get("navigator", {}).get("environment", {})
    if environment.get("CYRENE_TEAMS_PERSONAL_CHAT_ENABLED") != "true":
        return
    bot_app_id = environment.get("TEAMS_BOT_APP_ID")
    expected_policy = {
        "actor_subjects_file_env": "TEAMS_TASK_ACTOR_SUBJECTS_FILE",
        "workspace_id": environment.get("CYRENE_WORKSPACE_ID"),
        "issuer": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        "tenant_id": tenant_id,
        "bot_app_id": bot_app_id,
    }
    ingress = services.get("teams-ingress", {}).get("environment", {})
    provider = _json_environment_object(
        services.get("teams-provider", {}),
        "CYRENE_CAPABILITY_CONFIGURATION_JSON",
        "Teams provider",
    )
    exchange = _json_environment_object(
        services.get("exchange", {}),
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON",
        "Exchange integration",
    )
    connector_host = exchange.get("connectorHost")
    profile = connector_host.get("teamsMessages") if isinstance(connector_host, dict) else None
    subjects = profile.get("personalActorSubjects") if isinstance(profile, dict) else None
    if (
        not _uuid(bot_app_id)
        or ingress.get("TEAMS_PERSONAL_CHAT_ENABLED") != "true"
        or provider.get("personal_chat_enabled") is not True
        or provider.get("bot_app_id") != bot_app_id
        or provider.get("tenant_id") != tenant_id
        or not isinstance(subjects, list)
        or len(subjects) != len(actor_ids)
        or any(not _uuid(subject) for subject in subjects)
        or set(subjects) != actor_ids
        or profile.get("botAppId") != bot_app_id
        or profile.get("expectedIssuer") != expected_policy["issuer"]
        or profile.get("workspaceId") != expected_policy["workspace_id"]
        or principal_document.get("teams_private_conversations") != expected_policy
    ):
        raise PreflightError(
            "personal Teams transport must match the actor map, bot and Work policy"
        )


def _validate_automation_health_mount(config: dict[str, Any], source_bindings: Any) -> None:
    """Expose only the root-produced health projection through one read-only directory mount."""

    navigator = config.get("services", {}).get("navigator", {})
    volumes = navigator.get("volumes", []) if isinstance(navigator, dict) else []
    matches = [
        row
        for row in volumes
        if isinstance(row, dict) and row.get("target") == AUTOMATION_HEALTH_TARGET
    ]
    if len(matches) != 1:
        raise PreflightError(
            "Navigator must mount exactly one sanitized automation health directory"
        )
    mount = matches[0]
    source = mount.get("source")
    bind = mount.get("bind")
    try:
        path = Path(source) if isinstance(source, str) else Path("")
        metadata = path.lstat()
    except OSError as error:
        raise PreflightError("root-owned automation health directory is unavailable") from error
    if (
        mount.get("type") != "bind"
        or mount.get("read_only") is not True
        or bind != {"create_host_path": False}
        or not path.is_absolute()
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 10001
        or stat.S_IMODE(metadata.st_mode) != 0o750
    ):
        raise PreflightError(
            "automation health mount must be an exact root-owned read-only directory"
        )
    targets = source_bindings.get("targets", []) if isinstance(source_bindings, dict) else []
    for row in targets:
        if row.get("kind") == "host_health" and row.get("mountGid") != metadata.st_gid:
            raise PreflightError(
                "host-health selector group does not match the sanitized export directory"
            )


CONNECTOR_OPERATION_POLICIES = {
    "github.repository.list": ("github.repository.list", "profile", False),
    "github.repository.get": ("github.repository.get", "github_repository", False),
    "github.branches.list": ("github.branches.list", "github_repository", False),
    "github.issues.search": ("github.issues.search", "github_repository", False),
    "github.pull_requests.list": ("github.pull_requests.list", "github_repository", False),
    "github.pull_request.get": ("github.pull_request.get", "github_pull_request", False),
    "github.actions.runs.list": ("github.actions.runs.list", "github_repository", False),
    "github.actions.jobs.list": ("github.actions.jobs.list", "github_actions_run", False),
    "github.actions.job.logs.read": ("github.actions.job.logs.read", "github_actions_job", False),
    "github.ci.status": ("github.ci.status", "github_commit", False),
    "github.issue.get": ("github.issue.get", "github_issue", False),
    "github.branch.get": ("github.branch.get", "github_branch", False),
    "github.file.get": ("github.file.get", "github_file", False),
    "github.pull_request.draft.find": (
        "github.pull_request.draft.find",
        "github_pull_request_draft",
        False,
    ),
    "github.branch.create": ("github.branch.create", "github_branch", True),
    "github.branch.delete": ("github.branch.delete", "github_branch", True),
    "github.file.commit": ("github.file.commit", "github_file", True),
    "github.pull_request.draft.create": (
        "github.pull_request.draft.create",
        "github_pull_request_draft",
        True,
    ),
    "github.actions.rerun_failed": (
        "github.actions.rerun_failed",
        "github_actions_run",
        True,
    ),
    "github.issue.comment.create": ("github.issue.comment.create", "github_issue", True),
    "github.issue.state.update": ("github.issue.state.update", "github_issue", True),
    "github.vendor.cli.execute": ("github_vendor_cli", "profile", True),
    "azure_devops.observations.poll": (
        "azure_devops.observations.poll",
        "azure_devops_source",
        False,
    ),
    "microsoft_graph.planner.plan.get": (
        "microsoft_graph.planner.plan.get",
        "microsoft_graph_plan",
        False,
    ),
    "microsoft_graph.planner.tasks.list": (
        "microsoft_graph.planner.tasks.list",
        "microsoft_graph_plan",
        False,
    ),
    "computer.command.execute": ("computer.command.execute", "computer_workspace", True),
    "computer.file.read": ("computer.file.read", "computer_file", False),
    "computer.file.write": ("computer.file.write", "computer_file", True),
    "computer.directory.list": ("computer.directory.list", "computer_directory", False),
    "huggingface.models.list": ("huggingface.models.list", "profile", False),
    "huggingface.datasets.list": ("huggingface.datasets.list", "profile", False),
    "huggingface.spaces.list": ("huggingface.spaces.list", "profile", False),
    "huggingface.repo.get": ("huggingface.repo.get", "huggingface_repository", False),
    "huggingface.file.download": ("huggingface.file.download", "huggingface_repository", False),
    "huggingface.file.upload": ("huggingface.file.upload", "huggingface_repository", True),
    "huggingface.vendor.cli.execute": ("hf_vendor_cli", "profile", True),
    "cloud.provider.health.read": ("cloud_provider_health", "profile", False),
    "cloud.resources.list": ("cloud_resources_list", "cloud_target", False),
    "cloud.resource.get": ("cloud_resource_get", "cloud_target", False),
    "cloud.resource.create": ("cloud_resource_create", "cloud_target", True),
    "cloud.resource.deploy": ("cloud_resource_deploy", "cloud_target", True),
    "cloud.resource.start": ("cloud_resource_start", "cloud_target", True),
    "cloud.resource.stop": ("cloud_resource_stop", "cloud_target", True),
    "cloud.resource.restart": ("cloud_resource_restart", "cloud_target", True),
    "cloud.resource.update": ("cloud_resource_update", "cloud_target", True),
    "cloud.resource.delete": ("cloud_resource_delete", "cloud_target", True),
    "cloud.operation.status.read": ("cloud_operation_status", "cloud_operation", False),
    "cloud.operation.logs.read": ("cloud_operation_logs", "cloud_operation", False),
    "cloud.operation.cancel": ("cloud_operation_cancel", "cloud_operation", True),
    "cloud.vendor.cli.execute": ("cloud_vendor_cli", "profile", True),
}

# Provider-owned catalog inputs, including GitHub rerun evidence fields.
# Exchange derives computer workspace scope from its host-owned profile, outside tool schemas.
CONNECTOR_PROVIDER_REQUIRED_ARGUMENTS = {
    "azure_devops.observations.poll": ("target",),
    "microsoft_graph.planner.plan.get": ("plan_id",),
    "microsoft_graph.planner.tasks.list": ("plan_id",),
    "github.issues.search": ("repository_id", "repository"),
    "github.actions.jobs.list": ("repository_id", "repository", "run_id"),
    "github.actions.job.logs.read": (
        "repository_id",
        "repository",
        "run_id",
        "job_id",
    ),
    "github.branch.get": ("repository_id", "repository", "branch"),
    "github.branch.create": ("repository_id", "repository", "branch"),
    "github.branch.delete": ("repository_id", "repository", "branch"),
    "github.file.get": ("repository_id", "repository", "branch", "path"),
    "github.file.commit": ("repository_id", "repository", "branch", "path"),
    "github.pull_request.draft.find": (
        "repository_id",
        "repository",
        "head_branch",
        "base_branch",
    ),
    "github.pull_request.draft.create": (
        "repository_id",
        "repository",
        "head_branch",
        "base_branch",
    ),
    "github.actions.rerun_failed": (
        "repository_id",
        "repository",
        "run_id",
        "expected_workflow_id",
        "expected_head_branch",
        "expected_head_sha",
        "expected_attempt",
    ),
    "computer.command.execute": ("command",),
    "computer.file.read": ("path", "length"),
    "computer.file.write": ("path", "content_base64", "overwrite"),
    "computer.directory.list": ("path",),
}


def _validate_connector_bindings(
    path: Path, expected_providers: set[str], *, allow_empty: bool = False
) -> None:
    """Require globally unique Exchange connections and canonical operation maps."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreflightError("Navigator connector bindings are invalid JSON") from error
    _validate_connector_bindings_document(document, expected_providers, allow_empty=allow_empty)


def _validate_connector_bindings_document(
    document: Any, expected_providers: set[str], *, allow_empty: bool = False
) -> None:
    """Validate an already-read binding projection with the canonical operation map.

    中文：供文件预检和实时 Exchange 导出共用同一份绑定与只读操作校验。
    """

    if (
        not isinstance(document, dict)
        or set(document) != {"schemaVersion", "bindings"}
        or document.get("schemaVersion") != 1
        or isinstance(document.get("schemaVersion"), bool)
    ):
        raise PreflightError("Navigator connector bindings have an unsupported schema")
    bindings = document.get("bindings")
    if not isinstance(bindings, list) or (not bindings and not allow_empty):
        raise PreflightError("Navigator needs at least one Exchange-issued GitHub or HF binding")
    if not bindings and allow_empty:
        return
    seen_connection_refs: set[str] = set()
    seen_binding_ids: set[str] = set()
    supported_providers = {
        "github",
        "huggingface",
        "azure",
        "gcp",
        "runpod",
        "computer",
        "microsoft_graph",
        "azure_devops",
    }
    for binding in bindings:
        if (
            not isinstance(binding, dict)
            or set(binding) != {"bindingId", "connectionRef", "providerId", "profileRef", "tools"}
            or binding.get("providerId") not in supported_providers
        ):
            raise PreflightError("Navigator binding contains an unsupported provider")
        if PLACEHOLDER_RE.search(json.dumps(binding, sort_keys=True)):
            raise PreflightError("Navigator connector binding still contains a placeholder")
        profile = binding.get("profileRef")
        connection = binding.get("connectionRef")
        uuid_pattern = (
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        )
        if (
            not isinstance(profile, str)
            or not profile
            or not isinstance(connection, str)
            or not re.fullmatch(uuid_pattern, connection)
        ):
            raise PreflightError(
                "Navigator binding must use an Exchange-issued profile and connection UUID"
            )
        if binding.get("bindingId") != f"exchange-{binding['providerId']}-{connection}":
            raise PreflightError(
                "Navigator binding ID does not match the Exchange-issued connection/provider"
            )
        connection_key = connection.lower()
        if connection_key in seen_connection_refs:
            raise PreflightError("Navigator connection UUIDs must be unique across bindings")
        if binding["bindingId"] in seen_binding_ids:
            raise PreflightError("Navigator binding IDs must be unique")
        seen_connection_refs.add(connection_key)
        seen_binding_ids.add(binding["bindingId"])
        tools = binding.get("tools")
        if not isinstance(tools, list) or not tools:
            raise PreflightError("Navigator bindings need fixed tool-operation mappings")
        ids: set[str] = set()
        for tool in tools:
            if not isinstance(tool, dict) or set(tool) != {
                "toolId",
                "operationId",
                "resourceProjection",
                "mutating",
            }:
                raise PreflightError("Navigator tool map is malformed")
            if not all(
                isinstance(tool.get(field), str) and tool[field]
                for field in ("toolId", "operationId")
            ):
                raise PreflightError("Navigator tool ID and operation ID are required")
            if tool["toolId"] in ids:
                raise PreflightError("Navigator tool map contains duplicate tool IDs")
            expected = CONNECTOR_OPERATION_POLICIES.get(tool["operationId"])
            operation = tool["operationId"]
            provider = binding["providerId"]
            provider_matches = (
                (provider == "github" and operation.startswith("github."))
                or (provider == "huggingface" and operation.startswith("huggingface."))
                or (provider == "computer" and operation.startswith("computer."))
                or (provider in {"azure", "gcp", "runpod"} and operation.startswith("cloud."))
                or (provider == "microsoft_graph" and operation.startswith("microsoft_graph."))
                or (provider == "azure_devops" and operation.startswith("azure_devops."))
            )
            if (
                expected is None
                or not provider_matches
                or (operation == "cloud.resource.restart" and provider != "runpod")
                or (
                    tool.get("toolId"),
                    tool.get("resourceProjection"),
                    tool.get("mutating"),
                )
                != expected
            ):
                raise PreflightError(
                    f"Navigator mapping is not canonical for provider {provider} operation {operation}"
                )
            ids.add(tool["toolId"])
    providers = {row.get("providerId") for row in bindings if isinstance(row, dict)}
    if providers != expected_providers:
        raise PreflightError(
            f"connector binding providers must be exactly {sorted(expected_providers)} for this stage"
        )


def _validate_connector_tool_catalog(
    path: Path,
    bindings_path: Path,
    expected_providers: set[str],
    *,
    allow_empty: bool = False,
) -> None:
    """Bind Exchange tool schemas to the exact host map and required operation evidence."""

    try:
        catalog = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
        bindings = json.loads(
            bindings_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError(
            "Exchange connector tool catalog or binding map is invalid JSON"
        ) from error
    _validate_connector_tool_catalog_documents(
        catalog, bindings, expected_providers, allow_empty=allow_empty
    )


def _validate_connector_tool_catalog_documents(
    catalog: Any,
    bindings: Any,
    expected_providers: set[str],
    *,
    allow_empty: bool = False,
) -> None:
    """Validate native tool schemas against canonical binding operation maps.

    中文：供候选文件和运行中 Exchange 的真实目录快照复用同一校验。
    """

    if (
        not isinstance(catalog, dict)
        or set(catalog) != {"schemaVersion", "items"}
        or catalog.get("schemaVersion") != 1
        or isinstance(catalog.get("schemaVersion"), bool)
        or not isinstance(catalog.get("items"), list)
        or not isinstance(bindings, dict)
        or not isinstance(bindings.get("bindings"), list)
        or _catalog_contains_configuration_placeholder(catalog)
    ):
        raise PreflightError("Exchange connector tool catalog has an unsupported schema")
    items = catalog["items"]
    binding_rows = bindings["bindings"]
    if allow_empty and not items and not binding_rows:
        return
    if not items or len(items) != len(binding_rows):
        raise PreflightError("Exchange tool catalog must match every non-empty connector binding")
    by_id: dict[str, dict[str, Any]] = {}
    catalog_item_fields = {
        "bindingId",
        "capabilityId",
        "providerId",
        "connectionRef",
        "profileRef",
        "tools",
    }
    catalog_tool_fields = {"id", "name", "description", "readOnly", "inputSchema"}
    for item in items:
        if not isinstance(item, dict) or set(item) != catalog_item_fields:
            raise PreflightError("Exchange tool catalog item does not match its closed schema")
        binding_id = item.get("bindingId")
        if (
            not isinstance(binding_id, str)
            or not binding_id
            or binding_id in by_id
            or item.get("capabilityId") != "tool.provider.v1"
            or item.get("providerId") not in expected_providers
            or not isinstance(item.get("connectionRef"), str)
            or not isinstance(item.get("profileRef"), str)
            or not isinstance(item.get("tools"), list)
            or not item["tools"]
        ):
            raise PreflightError("Exchange tool catalog has an unbound provider or empty tool set")
        by_id[binding_id] = item

    bindings_by_id = {row.get("bindingId"): row for row in binding_rows if isinstance(row, dict)}
    if set(bindings_by_id) != set(by_id):
        raise PreflightError(
            "Exchange catalog binding IDs do not match Navigator connection bindings"
        )
    for binding_id, item in by_id.items():
        binding = bindings_by_id[binding_id]
        if any(
            item.get(field) != binding.get(field)
            for field in ("bindingId", "providerId", "connectionRef", "profileRef")
        ):
            raise PreflightError(
                "Exchange tool catalog connection scope does not match its binding"
            )
        binding_tools = {
            row["toolId"]: row for row in binding.get("tools", []) if isinstance(row, dict)
        }
        catalog_tools: dict[str, dict[str, Any]] = {}
        for tool in item["tools"]:
            if not isinstance(tool, dict) or set(tool) != catalog_tool_fields:
                raise PreflightError("Exchange tool catalog tool does not match its closed schema")
            tool_id = tool.get("id")
            schema = tool.get("inputSchema")
            if (
                not isinstance(tool_id, str)
                or not tool_id
                or tool_id in catalog_tools
                or not isinstance(tool.get("name"), str)
                or not tool["name"]
                or not isinstance(tool.get("description"), str)
                or not tool["description"]
                or not isinstance(tool.get("readOnly"), bool)
                or not isinstance(schema, dict)
                or schema.get("type") != "object"
            ):
                raise PreflightError("Exchange catalog tool metadata or input schema is invalid")
            if _schema_contains_credential_field(schema):
                raise PreflightError("provider tool schema must not accept credential fields")
            catalog_tools[tool_id] = tool
        if set(catalog_tools) != set(binding_tools):
            raise PreflightError(
                "Exchange catalog tool IDs do not match the canonical operation map"
            )
        for tool_id, mapped in binding_tools.items():
            if catalog_tools[tool_id]["readOnly"] is mapped["mutating"]:
                raise PreflightError(
                    "Exchange tool read/write metadata conflicts with the fixed operation map"
                )
            operation_id = mapped["operationId"]
            target_fields = CONNECTOR_PROVIDER_REQUIRED_ARGUMENTS.get(mapped["operationId"])
            if target_fields:
                schema = catalog_tools[tool_id]["inputSchema"]
                properties = schema.get("properties")
                required = schema.get("required")
                if (
                    not isinstance(properties, dict)
                    or not isinstance(required, list)
                    or any(
                        field not in properties or field not in required for field in target_fields
                    )
                ):
                    raise PreflightError(
                        "provider operation schema must require every canonical input and evidence argument"
                    )
            if operation_id == "github.actions.rerun_failed":
                _validate_github_rerun_input_schema(catalog_tools[tool_id]["inputSchema"])


def _catalog_contains_configuration_placeholder(catalog: Any) -> bool:
    """Reject machine-value placeholders while allowing angle-bracket tool prose."""

    if not isinstance(catalog, dict):
        return bool(PLACEHOLDER_RE.search(json.dumps(catalog, sort_keys=True)))

    filtered_catalog = dict(catalog)
    items = catalog.get("items")
    if isinstance(items, list):
        filtered_items: list[Any] = []
        for item in items:
            if not isinstance(item, dict):
                filtered_items.append(item)
                continue
            filtered_item = dict(item)
            tools = item.get("tools")
            if isinstance(tools, list):
                filtered_tools: list[Any] = []
                for tool in tools:
                    if isinstance(tool, dict):
                        filtered_tools.append(
                            {key: value for key, value in tool.items() if key != "description"}
                        )
                    else:
                        filtered_tools.append(tool)
                filtered_item["tools"] = filtered_tools
            filtered_items.append(filtered_item)
        filtered_catalog["items"] = filtered_items

    return bool(PLACEHOLDER_RE.search(json.dumps(filtered_catalog, sort_keys=True)))


def _validate_github_rerun_input_schema(schema: dict[str, Any]) -> None:
    """Require typed workflow/run evidence and a bounded assistant-branch locator."""

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise PreflightError("GitHub rerun schema must define typed expected run evidence")
    workflow_id = properties.get("expected_workflow_id")
    head_branch = properties.get("expected_head_branch")
    head_sha = properties.get("expected_head_sha")
    attempt = properties.get("expected_attempt")
    if (
        not isinstance(workflow_id, dict)
        or workflow_id.get("type") != "string"
        or workflow_id.get("pattern") != r"^[1-9][0-9]{0,19}$"
        or not isinstance(head_branch, dict)
        or head_branch.get("type") != "string"
        or head_branch.get("maxLength") != 255
        or not isinstance(head_sha, dict)
        or head_sha.get("type") != "string"
        or head_sha.get("pattern") != r"^[0-9a-fA-F]{40}$"
        or not isinstance(attempt, dict)
        or attempt.get("type") != "integer"
        or attempt.get("minimum") != 1
        or attempt.get("maximum") != 2_147_483_647
    ):
        raise PreflightError(
            "GitHub rerun schema must require a numeric workflow ID, bounded branch, exact SHA, and positive attempt"
        )


def _schema_contains_credential_field(value: Any) -> bool:
    """Reject catalog schemas that ask the model to supply a credential value."""

    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and SECRET_NAME_RE.search(key))
            or _schema_contains_credential_field(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_schema_contains_credential_field(item) for item in value)
    return False


def _huggingface_enabled(config: dict[str, Any]) -> bool:
    """Read the explicit HF deployment selection from the non-secret Compose label."""

    account_center = config.get("services", {}).get("account-center", {})
    labels = account_center.get("labels", {}) if isinstance(account_center, dict) else {}
    enabled = labels.get("com.cyrene.huggingface-enabled", "true")
    if enabled not in {"true", "false"}:
        raise PreflightError("CYRENE_HF_ENABLED must be exactly true or false")
    return enabled == "true"


def _automation_provider_ids(config: dict[str, Any]) -> set[str]:
    """Detect the explicit Graph/ADO plugin pair and reject a partially composed pair."""

    services = config.get("services", {})
    if not isinstance(services, dict):
        raise PreflightError("rendered Compose service inventory is malformed")
    present_services = AUTOMATION_PROVIDER_SERVICE_NAMES & set(services)
    secrets = config.get("secrets", {})
    secret_markers = {
        "account-center-graph-provider-hmac-key",
        "account-center-ado-provider-hmac-key",
        "exchange-graph-auth-context-hmac-key",
        "exchange-ado-auth-context-hmac-key",
    }
    has_secret_markers = isinstance(secrets, dict) and bool(secret_markers & set(secrets))
    if not present_services and not has_secret_markers:
        return set()
    if present_services != AUTOMATION_PROVIDER_SERVICE_NAMES or not has_secret_markers:
        raise PreflightError(
            "Microsoft Graph and Azure DevOps provider overlays must be enabled together"
        )
    if not isinstance(secrets, dict) or not secret_markers <= set(secrets):
        raise PreflightError("Graph/ADO provider overlay must define its complete signing-key set")
    return {"microsoft_graph", "azure_devops"}


def _expected_connector_providers(
    config: dict[str, Any], mode: str, connector_auth_phase: str | None = None
) -> set[str]:
    """Derive the only provider bindings that may be mounted for a deployment stage."""

    has_computer_runner = "computer-runtime" in config.get("services", {})
    providers = {"github"} | ({"huggingface"} if _huggingface_enabled(config) else set())
    automation_provider_ids = _automation_provider_ids(config)
    if mode == "connector-auth-readonly":
        if automation_provider_ids:
            raise PreflightError(
                "Microsoft Graph and Azure DevOps require their actor-owned OAuth/source setup, outside the generic connector auth evidence flow"
            )
        if connector_auth_phase == "provider-bootstrap":
            return {"github"} | ({"computer"} if has_computer_runner else set())
        if connector_auth_phase == "issued-connector-activation":
            services = config.get("services", {})
            cloud_service = (
                services.get("cloud-management-phase2") if isinstance(services, dict) else None
            )
            cloud_document = _json_environment_object(
                cloud_service if isinstance(cloud_service, dict) else {},
                "CYRENE_CAPABILITY_CONFIGURATION_JSON",
                "Cloud Management",
            )
            cloud_profiles = cloud_document.get("profiles")
            cloud_provider_ids = (
                {
                    row.get("provider")
                    for row in cloud_profiles
                    if isinstance(row, dict) and isinstance(row.get("provider"), str)
                }
                if isinstance(cloud_profiles, list)
                else set()
            )
            if cloud_provider_ids not in ({"azure", "gcp"}, {"azure", "gcp", "runpod"}) or len(
                cloud_provider_ids
            ) != len(cloud_profiles):
                raise PreflightError(
                    "activation provider set must include Azure/GCP and optional configured RunPod"
                )
            return providers | cloud_provider_ids | ({"computer"} if has_computer_runner else set())
        raise PreflightError("connector auth phase is required for connector-auth-readonly mode")
    if mode in {"stage1", "staging"}:
        return (
            providers | automation_provider_ids | ({"computer"} if has_computer_runner else set())
        )
    if mode == "stage2":
        return (
            providers
            | automation_provider_ids
            | {"azure", "gcp"}
            | ({"computer"} if has_computer_runner else set())
        )
    if mode == "stage3":
        return (
            providers
            | automation_provider_ids
            | {"azure", "gcp", "runpod"}
            | ({"computer"} if has_computer_runner else set())
        )
    providers.update(automation_provider_ids)
    providers.update({"computer"} if has_computer_runner else set())
    services = config.get("services", {})
    cloud_service = services.get("cloud-management-phase2")
    if isinstance(cloud_service, dict):
        providers.update({"azure", "gcp"})
    if isinstance(cloud_service, dict) and "runpod" in cloud_service.get("profiles", []):
        providers.add("runpod")
    return providers


def _canonical_json_digest(value: Any) -> str:
    """Hash one canonical UTF-8 JSON value without exposing its contents."""

    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_huggingface_repository_scope(value: Any, label: str) -> list[dict[str, Any]]:
    """Validate and canonically order one closed Hugging Face repository scope."""

    if not _valid_huggingface_repository_scope(value):
        raise PreflightError(f"{label} repository scope is invalid")
    return sorted(
        [
            {
                "repo_type": row["repo_type"],
                "repo_id": row["repo_id"],
                "permissions": sorted(row["permissions"]),
            }
            for row in value
        ],
        key=lambda row: (row["repo_type"], row["repo_id"]),
    )


def _normalize_exchange_huggingface_repository_scope(
    value: Any, label: str
) -> list[dict[str, Any]]:
    """Translate Exchange's canonical camelCase account-scope DTO to the shared scope shape."""

    if not isinstance(value, list):
        raise PreflightError(f"{label} repository scope is invalid")
    normalized: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"repoType", "repoId", "permissions"}:
            raise PreflightError(f"{label} repository scope is invalid")
        normalized.append(
            {
                "repo_type": row["repoType"],
                "repo_id": row["repoId"],
                "permissions": row["permissions"],
            }
        )
    return _normalize_huggingface_repository_scope(normalized, label)


def _json_environment_object(service: dict[str, Any], variable: str, label: str) -> dict[str, Any]:
    """Decode one Compose JSON setting while rejecting ambiguous keys and values."""

    environment = service.get("environment", {})
    encoded = environment.get(variable) if isinstance(environment, dict) else None
    if not isinstance(encoded, str):
        raise PreflightError(f"{label} configuration is missing")
    try:
        value = json.loads(
            encoded,
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise PreflightError(f"{label} configuration is invalid JSON") from error
    if not isinstance(value, dict) or PLACEHOLDER_RE.search(json.dumps(value, sort_keys=True)):
        raise PreflightError(f"{label} configuration is incomplete or contains a placeholder")
    return value


def _hf_seed_path(config: dict[str, Any]) -> Path:
    """Resolve the OAuth seed from the exact Account Center Compose secret mount."""

    source = config.get("secrets", {}).get("account-center-huggingface-oauth-source", {})
    path_text = source.get("file") if isinstance(source, dict) else None
    if not isinstance(path_text, str) or not path_text:
        raise PreflightError("HF OAuth seed is not mounted by Account Center")
    return Path(path_text)


def _connector_auth_scope_expectations(
    config: dict[str, Any],
    *,
    huggingface_seed_file: Path | None,
    runpod_key_file: Path | None,
) -> dict[str, Any]:
    """Build exact non-secret profile scope digests for read-only identity evidence."""

    services = config.get("services", {})
    cloud = services.get("cloud-management-phase2")
    huggingface = services.get("huggingface")
    exchange = services.get("exchange")
    if not all(isinstance(item, dict) for item in (cloud, huggingface, exchange)):
        raise PreflightError(
            "read-only provider bootstrap requires HF, cloud, and Exchange services"
        )
    cloud_environment = cloud.get("environment", {})
    cloud_document = _json_environment_object(
        cloud, "CYRENE_CAPABILITY_CONFIGURATION_JSON", "Cloud Management"
    )
    cloud_profiles = cloud_document.get("profiles")
    if not isinstance(cloud_profiles, list):
        raise PreflightError("Cloud Management must declare a closed provider profile list")
    cloud_by_provider = {
        row.get("provider"): row
        for row in cloud_profiles
        if isinstance(row, dict) and isinstance(row.get("provider"), str)
    }
    cloud_provider_ids = set(cloud_by_provider)
    if cloud_provider_ids not in ({"azure", "gcp"}, {"azure", "gcp", "runpod"}) or len(
        cloud_by_provider
    ) != len(cloud_profiles):
        raise PreflightError(
            "Cloud Management must contain Azure/GCP and only the explicitly configured optional RunPod profile"
        )
    if any(profile.get("read_only") is not True for profile in cloud_by_provider.values()):
        raise PreflightError("Azure, GCP, and RunPod profiles must enforce read_only=true")

    huggingface_document = _json_environment_object(
        huggingface, "CYRENE_CAPABILITY_CONFIGURATION_JSON", "Hugging Face"
    )
    if huggingface_document.get("read_only") is not True:
        raise PreflightError("Hugging Face provider must enforce read_only=true")
    expected_seed_path = _hf_seed_path(config)
    if huggingface_seed_file is None:
        raise PreflightError("the exact candidate HF OAuth seed path is required")
    try:
        supplied_seed_path = huggingface_seed_file.resolve(strict=True)
        expected_seed_path = expected_seed_path.resolve(strict=True)
        metadata = supplied_seed_path.lstat()
    except OSError as error:
        raise PreflightError("candidate HF OAuth seed is unavailable") from error
    if (
        supplied_seed_path != expected_seed_path
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or not _private_artifact(supplied_seed_path)
        or metadata.st_uid != 10001
        or metadata.st_mode & 0o077
    ):
        raise PreflightError("candidate HF seed must match the owner-only mounted OAuth file")
    _validate_huggingface_oauth_source(supplied_seed_path)
    hf_seed_sha256 = hashlib.sha256(supplied_seed_path.read_bytes()).hexdigest()
    try:
        seed_document = json.loads(
            supplied_seed_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError("candidate HF OAuth seed is invalid JSON") from error

    hf_repositories = _normalize_huggingface_repository_scope(
        seed_document.get("target_scope", {}).get("repositories"), "HF OAuth seed"
    )
    if any(row["permissions"] != ["read"] for row in hf_repositories):
        raise PreflightError("read-only HF OAuth seed may grant only read repository permissions")
    provider_repositories = _normalize_huggingface_repository_scope(
        huggingface_document.get("repositories"), "Hugging Face provider"
    )
    if provider_repositories != hf_repositories:
        raise PreflightError("HF OAuth seed and provider repositories scopes differ")

    exchange_document = _json_environment_object(
        exchange,
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON",
        "Exchange integration",
    )
    bootstrap_config = config.get("configs", {}).get("connector-bootstrap", {})
    bootstrap_path_text = (
        bootstrap_config.get("file") if isinstance(bootstrap_config, dict) else None
    )
    if not isinstance(bootstrap_path_text, str):
        raise PreflightError("private connector bootstrap config is missing")
    try:
        bootstrap_path = Path(bootstrap_path_text)
        bootstrap = json.loads(
            bootstrap_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError("private connector bootstrap config is invalid") from error
    hf_bootstrap = bootstrap.get("huggingface") if isinstance(bootstrap, dict) else None
    alias = hf_bootstrap.get("profileAlias") if isinstance(hf_bootstrap, dict) else None
    authorization_cloud = exchange_document.get("authorizationCloud")
    exchange_profiles = (
        authorization_cloud.get("profiles") if isinstance(authorization_cloud, dict) else None
    )
    exchange_hf_profile = (
        exchange_profiles.get(alias) if isinstance(exchange_profiles, dict) else None
    )
    account_scopes = (
        exchange_hf_profile.get("accountScopes") if isinstance(exchange_hf_profile, dict) else None
    )
    exchange_repositories = _normalize_exchange_huggingface_repository_scope(
        account_scopes.get("repositories") if isinstance(account_scopes, dict) else None,
        "Exchange HF profile",
    )
    if exchange_repositories != hf_repositories:
        raise PreflightError("HF OAuth, Exchange accountScopes, and provider repositories differ")
    repository_scope_digest = _canonical_json_digest(hf_repositories)

    huggingface_environment_binding = huggingface.get("environment", {}).get(
        "CYRENE_CAPABILITY_BINDING_ID"
    )
    if not isinstance(huggingface_environment_binding, str) or not huggingface_environment_binding:
        raise PreflightError("Hugging Face provider binding ID is missing")
    scope_inputs: dict[str, dict[str, Any]] = {
        "huggingface": {
            "bindingId": huggingface_environment_binding,
            "profileAlias": alias,
            "profile": exchange_hf_profile,
            "providerConfiguration": {
                key: value for key, value in huggingface_document.items() if key != "repositories"
            },
            "identitySelectors": {
                "authMode": seed_document.get("auth_mode"),
                "clientId": seed_document.get("client_id"),
                "repositoryScopeDigestSha256": repository_scope_digest,
                "credentialFileSha256": hf_seed_sha256,
            },
        }
    }
    secrets = config.get("secrets", {})
    secret_path_by_name = {
        name: Path(row["file"])
        for name, row in secrets.items()
        if isinstance(name, str) and isinstance(row, dict) and isinstance(row.get("file"), str)
    }
    cloud_binding_id = cloud_environment.get("CYRENE_CAPABILITY_BINDING_ID")
    if not isinstance(cloud_binding_id, str) or not cloud_binding_id:
        raise PreflightError("Cloud Management provider binding ID is missing")
    gcp_wif_file = secret_path_by_name.get("gcp-wif-credential-config")
    rendered_runpod_key_file = secret_path_by_name.get("runpod-api-key")
    runpod_enabled = "runpod" in cloud_provider_ids
    if gcp_wif_file is None:
        raise PreflightError("GCP WIF config must be mounted from its host file")
    if runpod_enabled and (rendered_runpod_key_file is None or runpod_key_file is None):
        raise PreflightError(
            "RunPod profile and explicit candidate key mount must be enabled together"
        )
    if not runpod_enabled and (rendered_runpod_key_file is not None or runpod_key_file is not None):
        raise PreflightError("an unconfigured RunPod profile must not accept a candidate key")
    try:
        gcp_wif_metadata = gcp_wif_file.lstat()
        gcp_wif_bytes = gcp_wif_file.read_bytes()
        gcp_wif_document = json.loads(gcp_wif_bytes, object_pairs_hook=_unique_json_object)
        if runpod_enabled:
            supplied_runpod_key_path = runpod_key_file.resolve(strict=True)
            rendered_runpod_key_path = rendered_runpod_key_file.resolve(strict=True)
            runpod_key_metadata = supplied_runpod_key_path.lstat()
            runpod_key_sha256 = hashlib.sha256(supplied_runpod_key_path.read_bytes()).hexdigest()
        else:
            supplied_runpod_key_path = None
            rendered_runpod_key_path = None
            runpod_key_metadata = None
            runpod_key_sha256 = None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError(
            "GCP WIF or configured RunPod credential metadata is invalid"
        ) from error
    if (
        (
            runpod_enabled
            and (
                supplied_runpod_key_path != rendered_runpod_key_path
                or not _private_artifact(supplied_runpod_key_path)
                or runpod_key_metadata.st_uid != 10001
                or stat.S_ISLNK(runpod_key_metadata.st_mode)
                or not stat.S_ISREG(runpod_key_metadata.st_mode)
                or runpod_key_metadata.st_mode & 0o077
                or runpod_key_metadata.st_size < 16
            )
        )
        or stat.S_ISLNK(gcp_wif_metadata.st_mode)
        or not stat.S_ISREG(gcp_wif_metadata.st_mode)
        or not isinstance(gcp_wif_document, dict)
        or gcp_wif_document.get("type") != "external_account"
    ):
        raise PreflightError(
            "GCP WIF or RunPod credential file does not match the private host contract"
        )
    exchange_profiles_by_provider: dict[str, dict[str, Any]] = {}
    if isinstance(exchange_profiles, dict):
        for profile in exchange_profiles.values():
            if isinstance(profile, dict) and profile.get("provider") in cloud_provider_ids:
                provider = profile["provider"]
                if provider in exchange_profiles_by_provider:
                    raise PreflightError(f"Exchange has multiple {provider} authorization profiles")
                exchange_profiles_by_provider[provider] = profile
    if set(exchange_profiles_by_provider) != cloud_provider_ids:
        raise PreflightError("Exchange cloud profiles differ from the configured provider set")
    for provider in sorted(cloud_provider_ids):
        profile = cloud_by_provider[provider]
        exchange_profile = exchange_profiles_by_provider.get(provider)
        if not isinstance(exchange_profile, dict):
            raise PreflightError(f"Exchange lacks the {provider} authorization profile")
        selectors: dict[str, Any] = {}
        if provider == "azure":
            selectors = {
                "clientId": cloud_environment.get("AZURE_CLIENT_ID"),
                "subscriptionId": cloud_environment.get("CYRENE_CLOUD_MGMT_AZURE_SUBSCRIPTION_ID"),
            }
        elif provider == "gcp":
            selectors = {
                "wifConfigPath": str(gcp_wif_file.resolve(strict=True)),
                "wifConfigSha256": hashlib.sha256(gcp_wif_bytes).hexdigest(),
                "wifAudience": gcp_wif_document.get("audience"),
                "workloadIdentityProvider": gcp_wif_document.get("audience"),
            }
        elif provider == "runpod":
            selectors = {
                "keyFilePath": str(supplied_runpod_key_path),
                "keyFileSizeBytes": runpod_key_metadata.st_size,
                "keyFileMode": stat.S_IMODE(runpod_key_metadata.st_mode),
                "keyFileSha256": runpod_key_sha256,
            }
        scope_inputs[provider] = {
            "bindingId": cloud_binding_id,
            "profile": profile,
            "exchangeProfile": exchange_profile,
            "identitySelectors": selectors,
        }
    digests = {provider: _canonical_json_digest(value) for provider, value in scope_inputs.items()}
    return {
        "expectedProviderScopeDigests": digests,
        "huggingfaceRepositoryScopeDigestSha256": repository_scope_digest,
    }


def _read_timestamp(value: Any, label: str) -> datetime:
    """Parse one UTC RFC 3339 timestamp without accepting naive or future values."""

    if not isinstance(value, str) or not value or len(value) > 64:
        raise PreflightError(f"{label} timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise PreflightError(f"{label} timestamp is malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PreflightError(f"{label} timestamp must use UTC")
    now = datetime.now(UTC)
    if parsed > now or now - parsed > timedelta(minutes=15):
        raise PreflightError(f"{label} evidence is outside the 15-minute freshness window")
    return parsed


def _validate_connector_auth_readonly_evidence(
    path: Path | None,
    *,
    expected_task_id: str | None,
    expected_owner: str,
    expected_scope_digests: dict[str, str],
) -> str:
    """Accept a fresh operator-recorded provider probe receipt without treating it as proof."""

    if path is None:
        raise PreflightError("fresh connector auth read-only evidence is required")
    if path.is_symlink() or not path.is_file() or not _private_artifact(path):
        raise PreflightError("connector auth evidence must be a regular private .artifacts file")
    metadata = path.stat()
    if metadata.st_uid != 10001 or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PreflightError("connector auth evidence must be UID 10001 mode 0600")
    try:
        raw = path.read_bytes()
        evidence = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError("connector auth evidence is invalid JSON") from error
    fields = {
        "schemaVersion",
        "status",
        "evidenceClass",
        "executionPath",
        "taskId",
        "owner",
        "verifiedAt",
        "providers",
    }
    execution_path = evidence.get("executionPath") if isinstance(evidence, dict) else None
    expected_operations_by_provider = (
        CONNECTOR_AUTH_READONLY_OPERATIONS_BY_PATH.get(execution_path)
        if isinstance(execution_path, str)
        else None
    )
    if (
        not isinstance(evidence, dict)
        or set(evidence) != fields
        or evidence.get("schemaVersion") != "cyrene-connector-auth-readonly-evidence-v1"
        or evidence.get("status") != "OPERATOR_ATTESTED"
        or evidence.get("evidenceClass") != "OPERATOR_RECORDED_REAL_PROBE"
        or expected_operations_by_provider is None
        or not isinstance(evidence.get("taskId"), str)
        or not evidence["taskId"]
        or (expected_task_id is not None and evidence["taskId"] != expected_task_id)
        or evidence.get("owner") != expected_owner
        or not isinstance(evidence.get("providers"), list)
    ):
        raise PreflightError(
            "connector auth evidence does not match the closed operator-recorded schema"
        )
    _read_timestamp(evidence.get("verifiedAt"), "connector auth verifiedAt")
    expected_providers = set(expected_scope_digests)
    providers: dict[str, dict[str, Any]] = {}
    for row in evidence["providers"]:
        if not isinstance(row, dict) or set(row) != {
            "providerId",
            "identityStatus",
            "authMode",
            "profileScopeDigestSha256",
            "readOperations",
        }:
            raise PreflightError("connector auth provider evidence has an unsupported schema")
        provider = row.get("providerId")
        if not isinstance(provider, str) or provider in providers:
            raise PreflightError("connector auth evidence has duplicate provider rows")
        providers[provider] = row
    if set(providers) != expected_providers:
        raise PreflightError(
            "connector auth evidence must include exactly the enabled provider profiles"
        )
    for provider, row in providers.items():
        if (
            row.get("identityStatus") != "OPERATOR_RECORDED"
            or row.get("authMode") not in CONNECTOR_AUTH_READONLY_AUTH_MODES[provider]
            or row.get("profileScopeDigestSha256") != expected_scope_digests[provider]
        ):
            raise PreflightError(
                f"{provider} operator-recorded identity receipt does not match its profile scope"
            )
        operations = row.get("readOperations")
        expected_operations = expected_operations_by_provider.get(provider)
        if expected_operations is None:
            raise PreflightError(
                f"{provider} evidence is unsupported for execution path {execution_path}"
            )
        if not isinstance(operations, list) or len(operations) != len(expected_operations):
            raise PreflightError(f"{provider} evidence must contain its fixed read-only operations")
        observed_operations: set[str] = set()
        for operation in operations:
            if not isinstance(operation, dict) or set(operation) != {
                "operationId",
                "outcome",
                "observedAt",
                "resultDigestSha256",
            }:
                raise PreflightError(
                    f"{provider} read operation evidence has an unsupported schema"
                )
            operation_id = operation.get("operationId")
            digest = operation.get("resultDigestSha256")
            if (
                not isinstance(operation_id, str)
                or operation_id in observed_operations
                or operation_id not in expected_operations
                or operation.get("outcome") != "SUCCEEDED"
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise PreflightError(
                    f"{provider} operation evidence is not a fixed successful read"
                )
            observed_operations.add(operation_id)
            observed_at = _read_timestamp(operation.get("observedAt"), f"{provider} {operation_id}")
            verified_at = datetime.fromisoformat(
                evidence["verifiedAt"][:-1] + "+00:00"
                if evidence["verifiedAt"].endswith("Z")
                else evidence["verifiedAt"]
            )
            if observed_at > verified_at:
                raise PreflightError(
                    f"{provider} read operation occurred after evidence verification"
                )
        if observed_operations != expected_operations:
            raise PreflightError(f"{provider} evidence is missing a required read operation")
    return hashlib.sha256(raw).hexdigest()


def _validate_new_provider_bindings_readonly(
    document: dict[str, Any], expected_provider_ids: set[str]
) -> None:
    """Require exact issued HF/cloud bindings with a read-only Exchange operation set."""

    bindings = document.get("bindings") if isinstance(document, dict) else None
    if not isinstance(bindings, list):
        raise PreflightError("connector binding document is malformed")
    if not expected_provider_ids <= {"huggingface", "azure", "gcp", "runpod"}:
        raise PreflightError("activation provider set contains an unsupported connector")
    selected = [
        row
        for row in bindings
        if isinstance(row, dict) and row.get("providerId") in expected_provider_ids
    ]
    by_provider = {row["providerId"]: row for row in selected}
    if len(selected) != len(expected_provider_ids) or set(by_provider) != expected_provider_ids:
        raise PreflightError(
            "activation must use exactly one Exchange-issued binding for every configured provider"
        )
    for provider in expected_provider_ids:
        binding = by_provider[provider]
        tools = binding.get("tools")
        allowed_operations = CONNECTOR_MODEL_READONLY_OPERATIONS[provider]
        if not isinstance(tools, list) or not tools:
            raise PreflightError(f"{provider} read-only binding must expose at least one safe read")
        operations: set[str] = set()
        for tool in tools:
            operation = tool.get("operationId") if isinstance(tool, dict) else None
            if (
                not isinstance(operation, str)
                or operation not in allowed_operations
                or tool.get("mutating") is not False
                or operation in operations
            ):
                raise PreflightError(
                    f"{provider} binding contains a write, CLI, duplicate, or non-allowlisted operation"
                )
            operations.add(operation)


def _validate_huggingface_profile_selection(compose: dict[str, Any], mode: str) -> bool:
    """Require the HF overlay to match its explicit selection and phase."""

    huggingface_enabled = _huggingface_enabled(compose)
    has_huggingface_service = "huggingface" in compose.get("services", {})
    if huggingface_enabled != has_huggingface_service:
        if huggingface_enabled:
            raise PreflightError(
                "HF-enabled deployment requires compose.huggingface.yaml and its real OAuth seed"
            )
        raise PreflightError(
            "GitHub-only deployment must not include the HF provider service or its secret overlay"
        )
    if not huggingface_enabled and mode not in {"bootstrap", "staging", "stage1"}:
        raise PreflightError("Hugging Face may be disabled only for bootstrap and stage-1 staging")
    return huggingface_enabled


def _network_names(service: Any, label: str) -> set[str]:
    """Read one Compose service's network set without accepting malformed declarations."""

    networks = service.get("networks", {}) if isinstance(service, dict) else None
    if isinstance(networks, dict) and all(isinstance(name, str) for name in networks):
        return set(networks)
    if isinstance(networks, list) and all(isinstance(name, str) for name in networks):
        return set(networks)
    raise PreflightError(f"{label} network declaration is malformed")


def _validate_ado_source_activation(sources: Any) -> None:
    """Check bounded ADO source selections without treating them as OAuth/source ACL proof."""

    source_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise PreflightError("Azure DevOps activation source is malformed")
        kind = source.get("kind")
        common = {"source_id", "kind", "project_id", "project_name", "profile_ref"}
        allowed_by_kind = {
            "project_pipelines": common,
            "pipeline_runs": common | {"pipeline_id"},
            "release_deployments": common | {"definition_id", "definition_environment_id"},
        }
        expected = allowed_by_kind.get(kind)
        if expected is None:
            raise PreflightError("Azure DevOps activation has an unsupported source kind")
        required = expected
        if kind == "release_deployments":
            required = common
            if "definition_environment_id" in source:
                required = required | {"definition_id", "definition_environment_id"}
        if set(source) != required:
            raise PreflightError("Azure DevOps source contains an unsupported or missing field")
        source_id = source.get("source_id")
        if (
            not isinstance(source_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", source_id)
            or source_id in source_ids
            or not _uuid(source.get("project_id"))
            or not _uuid(source.get("profile_ref"))
            or not isinstance(source.get("project_name"), str)
            or not 1 <= len(source["project_name"]) <= 128
            or any(ord(char) < 32 for char in source["project_name"])
        ):
            raise PreflightError("Azure DevOps source identity or project metadata is invalid")
        source_ids.add(source_id)
        numeric_fields = (
            ("pipeline_id",)
            if kind == "pipeline_runs"
            else ("definition_id", "definition_environment_id")
            if kind == "release_deployments"
            else ()
        )
        if any(
            type(source[field]) is not int or not 1 <= source[field] <= 2_147_483_647
            for field in numeric_fields
        ):
            raise PreflightError("Azure DevOps numeric source filters are invalid")


def _validate_automation_provider_topology(config: dict[str, Any]) -> bool:
    """Validate the opt-in Graph/ADO mTLS pair and their bounded process envelope."""

    if not _automation_provider_ids(config):
        return False
    services = config["services"]
    networks = config.get("networks", {})
    account_center = services.get("account-center")
    exchange = services.get("exchange")
    if not isinstance(account_center, dict) or not isinstance(exchange, dict):
        raise PreflightError("Graph/ADO providers require the existing Account Center and Exchange")
    account_map = _parse_secret_file_map(account_center, "Account Center")
    exchange_map = _parse_secret_file_map(exchange, "Exchange")

    providers = (
        {
            "service": "microsoft-graph",
            "plugin": "cyrene.providers.microsoft-graph",
            "caller": "microsoft-graph-provider",
            "exchange_env": "EXCHANGE_GRAPH_AUTH_CONTEXT_HMAC_KEY",
            "provider_env": "GRAPH_ACCOUNT_CENTER_PROVIDER_HMAC_KEY",
            "root_env": "GRAPH_ACCOUNT_CENTER_ROOT_CERTIFICATE",
            "cert_env": "GRAPH_ACCOUNT_CENTER_CLIENT_CERTIFICATE",
            "key_env": "GRAPH_ACCOUNT_CENTER_CLIENT_KEY",
            "exchange_ref": "microsoft-graph/exchange-verification-key",
            "provider_ref": "microsoft-graph/account-center-provider-signing-key",
            "root_ref": "microsoft-graph/account-center-root-certificate",
            "cert_ref": "microsoft-graph/account-center-client-certificate",
            "key_ref": "microsoft-graph/account-center-client-key",
            "exchange_secret": "exchange-graph-auth-context-hmac-key",
            "provider_secret": "account-center-graph-provider-hmac-key",
            "server_cert": "microsoft-graph-server-cert",
            "server_key": "microsoft-graph-server-key",
            "health_cert": "microsoft-graph-health-client-cert",
            "health_key": "microsoft-graph-health-client-key",
            "account_cert": "microsoft-graph-account-center-client-cert",
            "account_key": "microsoft-graph-account-center-client-key",
            "health_ca": "exchange-plugin-root-ca",
            "exchange_network": "graph-exchange-private",
            "account_network": "graph-account-center-private",
            "egress_network": "graph-egress",
        },
        {
            "service": "azure-devops-observation",
            "plugin": "cyrene.providers.azure-devops-observation",
            "caller": "azure-devops-observation-provider",
            "exchange_env": "EXCHANGE_ADO_AUTH_CONTEXT_HMAC_KEY",
            "provider_env": "ADO_ACCOUNT_CENTER_PROVIDER_HMAC_KEY",
            "root_env": "ADO_ACCOUNT_CENTER_ROOT_CERTIFICATE",
            "cert_env": "ADO_ACCOUNT_CENTER_CLIENT_CERTIFICATE",
            "key_env": "ADO_ACCOUNT_CENTER_CLIENT_KEY",
            "exchange_ref": "azure-devops-observation/exchange-verification-key",
            "provider_ref": "azure-devops-observation/account-center-provider-signing-key",
            "root_ref": "azure-devops-observation/account-center-root-certificate",
            "cert_ref": "azure-devops-observation/account-center-client-certificate",
            "key_ref": "azure-devops-observation/account-center-client-key",
            "exchange_secret": "exchange-ado-auth-context-hmac-key",
            "provider_secret": "account-center-ado-provider-hmac-key",
            "server_cert": "azure-devops-observation-server-cert",
            "server_key": "azure-devops-observation-server-key",
            "health_cert": "azure-devops-observation-health-client-cert",
            "health_key": "azure-devops-observation-health-client-key",
            "account_cert": "azure-devops-observation-account-center-client-cert",
            "account_key": "azure-devops-observation-account-center-client-key",
            "health_ca": "exchange-plugin-root-ca",
            "exchange_network": "ado-exchange-private",
            "account_network": "ado-account-center-private",
            "egress_network": "ado-egress",
        },
    )
    for row in providers:
        service = services[row["service"]]
        environment = service.get("environment", {})
        expected_map = {
            row["exchange_env"]: f"/run/secrets/{row['exchange_secret']}",
            row["provider_env"]: f"/run/secrets/{row['provider_secret']}",
            row["root_env"]: "/run/secrets/exchange-plugin-root-ca",
            row["cert_env"]: f"/run/secrets/{row['account_cert']}",
            row["key_env"]: f"/run/secrets/{row['account_key']}",
        }
        expected_secrets = {
            (row["exchange_secret"], row["exchange_secret"]),
            (row["provider_secret"], row["provider_secret"]),
            ("exchange-plugin-root-ca", "exchange-plugin-root-ca"),
            ("exchange-plugin-client-ca", f"{row['service']}-client-ca"),
            (row["server_cert"], row["server_cert"]),
            (row["server_key"], row["server_key"]),
            (row["health_cert"], row["health_cert"]),
            (row["health_key"], row["health_key"]),
            (row["account_cert"], row["account_cert"]),
            (row["account_key"], row["account_key"]),
        }
        required_networks = {row["exchange_network"], row["account_network"], row["egress_network"]}
        binding_id = (
            environment.get("CYRENE_CAPABILITY_BINDING_ID")
            if isinstance(environment, dict)
            else None
        )
        if (
            service.get("pull_policy") != "never"
            or service.get("restart") != "unless-stopped"
            or service.get("read_only") is not True
            or "ALL" not in service.get("cap_drop", [])
            or service.get("cap_add")
            or "no-new-privileges:true" not in service.get("security_opt", [])
            or service.get("tmpfs") != ["/tmp:rw,nosuid,nodev,noexec,size=16m"]
            or service.get("privileged") is True
            or service.get("ports")
            or service.get("expose") != ["7443"]
            or service.get("volumes")
            or service.get("configs")
            or _memory_limit_bytes(service.get("mem_limit")) != 192 * MIB
            or _memory_limit_bytes(service.get("memswap_limit")) != 192 * MIB
            or _cpu_limit_decimal(service.get("cpus")) != Decimal("0.10")
            or service.get("pids_limit") != 96
            or _network_names(service, row["service"]) != required_networks
            or service.get("depends_on", {}).get("account-center", {}).get("condition")
            != "service_healthy"
            or _secret_mount_pairs(service, row["service"]) != expected_secrets
            or not isinstance(binding_id, str)
            or not binding_id
            or PLACEHOLDER_RE.search(binding_id)
            or _parse_secret_file_map(service, row["service"]) != expected_map
        ):
            raise PreflightError(
                f"{row['service']} must keep the fixed bounded mTLS provider envelope and secret set"
            )
        provider_config = _json_environment_object(
            service, "CYRENE_CAPABILITY_CONFIGURATION_JSON", row["service"]
        )
        account_profile = provider_config.get("account_center")
        expected_fields = (
            {
                "exchange_caller_id",
                "exchange_verification_key_ref",
                "account_center",
                "read_only",
                "timeout_seconds",
                "max_page_size",
                "sharepointTargets",
                "hostSecretRefs",
            }
            if row["service"] == "microsoft-graph"
            else {
                "binding_id",
                "exchange_caller_id",
                "exchange_verification_key_ref",
                "account_center",
                "organization",
                "tenant_id",
                "authorized_sources",
                "timeout_seconds",
                "hostSecretRefs",
            }
        )
        expected_mtls_refs = {
            "root_cert_ref": row["root_ref"],
            "client_cert_ref": row["cert_ref"],
            "client_key_ref": row["key_ref"],
        }
        if row["service"] == "microsoft-graph":
            if set(provider_config) - expected_fields:
                raise PreflightError(
                    "Microsoft Graph activation contains an unsupported host field"
                )
        else:
            if (
                set(provider_config) - expected_fields
                or provider_config.get("binding_id") not in {None, binding_id}
                or not isinstance(provider_config.get("organization"), str)
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", provider_config["organization"]
                )
                or not _uuid(provider_config.get("tenant_id"))
                or not isinstance(provider_config.get("authorized_sources"), list)
                or not 1 <= len(provider_config["authorized_sources"]) <= 100
            ):
                raise PreflightError(
                    "Azure DevOps activation must pin a tenant, organization and source list"
                )
            _validate_ado_source_activation(provider_config["authorized_sources"])
        expected_host_refs = {
            row["exchange_ref"]: row["exchange_env"],
            row["provider_ref"]: row["provider_env"],
            row["root_ref"]: row["root_env"],
            row["cert_ref"]: row["cert_env"],
            row["key_ref"]: row["key_env"],
        }
        if (
            provider_config.get("exchange_caller_id") != "exchange-service"
            or provider_config.get("exchange_verification_key_ref") != row["exchange_ref"]
            or not isinstance(account_profile, dict)
            or account_profile.get("connection_ref") != "grpcs://account-center:7443"
            or account_profile.get("transport") != "mtls"
            or account_profile.get("caller_id") != row["caller"]
            or account_profile.get("signing_key_ref") != row["provider_ref"]
            or account_profile.get("mtls") != expected_mtls_refs
            or provider_config.get("hostSecretRefs") != expected_host_refs
        ):
            raise PreflightError(f"{row['service']} must use its fixed Account Center mTLS profile")
        healthcheck = service.get("healthcheck")
        test = healthcheck.get("test") if isinstance(healthcheck, dict) else None
        command = " ".join(str(part) for part in test) if isinstance(test, list) else ""
        if any(
            fragment not in command
            for fragment in (
                "/usr/local/bin/cyrene-plugin-grpc-health",
                f"{row['service']}:7443",
                row["plugin"],
                f"/run/secrets/{row['health_ca']}",
                f"/run/secrets/{row['health_cert']}",
                f"/run/secrets/{row['health_key']}",
            )
        ):
            raise PreflightError(f"{row['service']} must use its fixed gRPC mTLS health probe")
        for network_name in (row["exchange_network"], row["account_network"]):
            network = networks.get(network_name) if isinstance(networks, dict) else None
            if not isinstance(network, dict) or network.get("internal") is not True:
                raise PreflightError(
                    "Graph/ADO Account Center and Exchange networks must be internal"
                )
        egress = networks.get(row["egress_network"]) if isinstance(networks, dict) else None
        if not isinstance(egress, dict) or egress.get("internal") is True:
            raise PreflightError("Graph/ADO provider egress must use a separate outbound network")

    for name, required_networks in (
        ("account-center", {"graph-account-center-private", "ado-account-center-private"}),
        ("exchange", {"graph-exchange-private", "ado-exchange-private"}),
    ):
        service = services[name]
        if not required_networks <= _network_names(service, name):
            raise PreflightError(f"{name} must join the Graph/ADO private provider networks")
        mapping = account_map if name == "account-center" else exchange_map
        mounted = {f"/run/secrets/{target}" for target in _service_secret_targets(service)}
        if not set(mapping.values()) <= mounted:
            raise PreflightError(f"{name} secret-file map references an unmounted secret")
    exchange_dependencies = exchange.get("depends_on", {})
    if any(
        not isinstance(exchange_dependencies.get(name), dict)
        or exchange_dependencies[name].get("condition") != "service_healthy"
        for name in AUTOMATION_PROVIDER_SERVICE_NAMES
    ):
        raise PreflightError("Exchange must wait for both Graph/ADO gRPC health probes")
    expected_account_mappings = {
        "CYRENE_ACCOUNT_CENTER_GRAPH_PROVIDER_HMAC_KEY": "/run/secrets/account-center-graph-provider-hmac-key",
        "CYRENE_ACCOUNT_CENTER_ADO_PROVIDER_HMAC_KEY": "/run/secrets/account-center-ado-provider-hmac-key",
    }
    expected_exchange_mappings = {
        "EXCHANGE_GRAPH_AUTH_CONTEXT_HMAC_KEY": "/run/secrets/exchange-graph-auth-context-hmac-key",
        "EXCHANGE_ADO_AUTH_CONTEXT_HMAC_KEY": "/run/secrets/exchange-ado-auth-context-hmac-key",
    }
    if (
        any(account_map.get(key) != value for key, value in expected_account_mappings.items())
        or any(exchange_map.get(key) != value for key, value in expected_exchange_mappings.items())
        or not {
            ("account-center-graph-provider-hmac-key", "account-center-graph-provider-hmac-key"),
            ("account-center-ado-provider-hmac-key", "account-center-ado-provider-hmac-key"),
        }
        <= _secret_mount_pairs(account_center, "Account Center")
        or not {
            ("exchange-graph-auth-context-hmac-key", "exchange-graph-auth-context-hmac-key"),
            ("exchange-ado-auth-context-hmac-key", "exchange-ado-auth-context-hmac-key"),
        }
        <= _secret_mount_pairs(exchange, "Exchange")
    ):
        raise PreflightError(
            "Graph/ADO signing and Exchange context keys must remain separately mounted"
        )
    return True


def _validate_provider_listener_addresses(compose: dict[str, Any]) -> None:
    """Require multi-network DirectPlugin providers to bind all container interfaces.

    中文：HF 与 Cloud Management 位于多网卡容器，监听地址必须覆盖全部接口。
    """

    services = compose.get("services", {})
    required_services = {"account-center", "github-issues", "teams-provider"}
    provider_services = {
        "huggingface",
        "cloud-management-phase2",
        *AUTOMATION_PROVIDER_SERVICE_NAMES,
    }
    for service_name in sorted(required_services | provider_services):
        service = services.get(service_name)
        if service is None and service_name in provider_services:
            continue
        if service is None:
            raise PreflightError(f"{service_name} must declare its fixed provider command")
        command = service.get("command") if isinstance(service, dict) else None
        if not isinstance(command, list) or any(not isinstance(part, str) for part in command):
            raise PreflightError(f"{service_name} must declare its fixed provider command")
        listen_indexes = [index for index, part in enumerate(command) if part == "--listen"]
        if (
            len(listen_indexes) != 1
            or listen_indexes[0] + 1 >= len(command)
            or command[listen_indexes[0] + 1] != "0.0.0.0:7443"
        ):
            raise PreflightError(
                f"{service_name} must listen on 0.0.0.0:7443 for all private networks"
            )


def _validate_profile_stage(compose: dict[str, Any], mode: str) -> None:
    """Verify that optional provider profiles are attached only in their stage overlay."""

    services = compose.get("services", {})
    _validate_huggingface_profile_selection(compose, mode)
    _validate_exchange_profile_stage(compose, mode)
    _validate_computer_runtime_boundary(compose)
    _validate_automation_provider_topology(compose)
    if mode in {"staging", "stage1"} and ("cloud-management-phase2" in services):
        raise PreflightError(
            "cloud provider services must not be included before the preceding-stage evidence"
        )
    if mode == "stage2":
        row = services.get("cloud-management-phase2")
        if not isinstance(row, dict) or "cloud-phase2" not in row.get("profiles", []):
            raise PreflightError("phase 2 must use the dedicated cloud-phase2 Compose overlay")
        config_text = row.get("environment", {}).get("CYRENE_CAPABILITY_CONFIGURATION_JSON", "")
        _verify_provider_profiles(config_text, {"azure", "gcp"})
        _verify_uami(row.get("environment", {}).get("AZURE_CLIENT_ID"))
        _verify_azure_cli_scope(row)
    if mode == "stage3":
        row = services.get("cloud-management-phase2")
        if not isinstance(row, dict) or "runpod" not in row.get("profiles", []):
            raise PreflightError("phase 3 must use the dedicated RunPod Compose overlay")
        config_text = row.get("environment", {}).get("CYRENE_CAPABILITY_CONFIGURATION_JSON", "")
        _verify_provider_profiles(config_text, {"azure", "gcp", "runpod"})
        _verify_uami(row.get("environment", {}).get("AZURE_CLIENT_ID"))
        _verify_azure_cli_scope(row)
        if (
            row.get("environment", {}).get("CYRENE_CLOUD_MGMT_RUNPOD_API_KEY_FILE")
            != "/run/secrets/runpod-api-key"
        ):
            raise PreflightError(
                "RunPod CLI must read its key from the stage-3 mounted secret file"
            )
    if mode == "connector-auth-readonly":
        row = services.get("cloud-management-phase2")
        if not isinstance(row, dict) or "cloud-phase2" not in row.get("profiles", []):
            raise PreflightError("credential read-only activation must use cloud-phase2 profile")
        runpod_enabled = "runpod" in row.get("profiles", [])
        expected_cloud = {"azure", "gcp"} | ({"runpod"} if runpod_enabled else set())
        _verify_provider_profiles(
            row.get("environment", {}).get("CYRENE_CAPABILITY_CONFIGURATION_JSON", ""),
            expected_cloud,
        )
        _verify_uami(row.get("environment", {}).get("AZURE_CLIENT_ID"))
        _verify_azure_cli_scope(row)
        if runpod_enabled:
            if (
                row.get("environment", {}).get("CYRENE_CLOUD_MGMT_RUNPOD_API_KEY_FILE")
                != "/run/secrets/runpod-api-key"
            ):
                raise PreflightError(
                    "configured RunPod CLI must read its key from the candidate secret mount"
                )
        elif row.get("environment", {}).get("CYRENE_CLOUD_MGMT_RUNPOD_API_KEY_FILE"):
            raise PreflightError("unconfigured RunPod must not enter the Cloud Management profile")
    if mode == "cutover":
        cloud_service = services.get("cloud-management-phase2")
        if cloud_service:
            expected = (
                {"azure", "gcp", "runpod"}
                if "runpod" in cloud_service.get("profiles", [])
                else {"azure", "gcp"}
            )
            _verify_provider_profiles(
                cloud_service.get("environment", {}).get(
                    "CYRENE_CAPABILITY_CONFIGURATION_JSON", ""
                ),
                expected,
            )
            _verify_uami(cloud_service.get("environment", {}).get("AZURE_CLIENT_ID"))
            _verify_azure_cli_scope(cloud_service)
            if (
                "runpod" in expected
                and cloud_service.get("environment", {}).get(
                    "CYRENE_CLOUD_MGMT_RUNPOD_API_KEY_FILE"
                )
                != "/run/secrets/runpod-api-key"
            ):
                raise PreflightError(
                    "RunPod CLI must read its key from the stage-3 mounted secret file"
                )


def _validate_exchange_authorization_cloud_runner_phase(
    config: dict[str, Any], connector_auth_phase: str
) -> None:
    """Keep the single cloud monitor off during bootstrap and on after issuance."""

    exchange = config.get("services", {}).get("exchange", {})
    environment = exchange.get("environment", {}) if isinstance(exchange, dict) else {}
    observed = environment.get("CYRENE_EXCHANGE_AUTHORIZATION_CLOUD_RUNNER_ENABLED", "false")
    expected = "false" if connector_auth_phase == "provider-bootstrap" else "true"
    if connector_auth_phase not in CONNECTOR_AUTH_READONLY_PHASES or observed != expected:
        raise PreflightError(
            f"Exchange AuthorizationCloudRunner must be {expected} during {connector_auth_phase}"
        )


def _validate_computer_runtime_boundary(compose: dict[str, Any]) -> None:
    """Require a low-privilege runner and mTLS proxy with no credentials on the runner."""

    services = compose.get("services", {})
    runner = services.get("computer-runtime")
    proxy = services.get("computer-runtime-proxy")
    if runner is None and proxy is None:
        return
    if not isinstance(runner, dict) or not isinstance(proxy, dict):
        raise PreflightError("computer runtime and its mTLS proxy must be deployed together")

    runner_environment = runner.get("environment", {})
    expected_environment = {
        "CYRENE_COMPUTER_WORKSPACE_ROOT": "/workspace",
        "CYRENE_COMPUTER_ARTIFACT_ROOT": "/var/lib/cyrene/artifacts",
        "CYRENE_COMPUTER_COMMAND_DEFAULT_TIMEOUT_MS": "30000",
        "CYRENE_COMPUTER_COMMAND_MAX_TIMEOUT_MS": "600000",
        "CYRENE_COMPUTER_MAX_OUTPUT_BYTES": "65536",
        "CYRENE_COMPUTER_MAX_FILE_BYTES": "1048576",
        "CYRENE_COMPUTER_MAX_DIRECTORY_ENTRIES": "2048",
        "CYRENE_COMPUTER_MAX_DIRECTORY_DEPTH": "32",
        "CYRENE_COMPUTER_MAX_ARTIFACT_BYTES": "16777216",
    }
    if (
        not isinstance(runner_environment, dict)
        or set(runner_environment) != set(expected_environment)
        or any(runner_environment.get(key) != value for key, value in expected_environment.items())
    ):
        raise PreflightError(
            "computer runtime resource and file limits do not match the approved transport cap"
        )
    runner_volumes = runner.get("volumes", [])
    expected_tmpfs = {
        "/tmp:rw,nosuid,nodev,size=64m",
        "/workspace:rw,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=192m",
        "/var/lib/cyrene/artifacts:rw,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=64m",
    }
    if (
        runner.get("user") != "10001:10001"
        or runner.get("read_only") is not True
        or "ALL" not in runner.get("cap_drop", [])
        or runner.get("cap_add")
        or "no-new-privileges:true" not in runner.get("security_opt", [])
        or set(runner.get("tmpfs", [])) != expected_tmpfs
        or _memory_limit_bytes(runner.get("mem_limit")) != 512 * MIB
        or _cpu_limit_decimal(runner.get("cpus")) != Decimal("0.10")
        or runner.get("secrets")
        or runner.get("configs")
        or not isinstance(runner_volumes, list)
        or runner_volumes
        or set(runner.get("networks", {})) != {"computer-runtime-private"}
        or runner.get("ports")
    ):
        raise PreflightError(
            "computer runner must use bounded scratch tmpfs, no credentials, and one private network"
        )
    networks = compose.get("networks", {})
    if any(
        not isinstance(networks.get(name), dict) or networks[name].get("internal") is not True
        for name in ("computer-runtime-private", "exchange-computer-private")
    ):
        raise PreflightError("computer runner and Exchange proxy networks must be internal-only")

    proxy_networks = set(proxy.get("networks", {}))
    proxy_volumes = proxy.get("volumes", [])
    if (
        proxy_networks != {"computer-runtime-private", "exchange-computer-private"}
        or proxy.get("ports")
        or proxy.get("privileged") is True
        or proxy.get("read_only") is not True
        or "ALL" not in proxy.get("cap_drop", [])
        or proxy.get("cap_add")
        or "no-new-privileges:true" not in proxy.get("security_opt", [])
        or _memory_limit_bytes(proxy.get("mem_limit")) != 64 * MIB
        or _cpu_limit_decimal(proxy.get("cpus")) != Decimal("0.05")
        or proxy.get("secrets") is None
        or {row.get("source") for row in proxy.get("secrets", []) if isinstance(row, dict)}
        != {
            "computer-runtime-proxy-server-cert",
            "computer-runtime-proxy-server-key",
            "exchange-plugin-client-ca",
        }
    ):
        raise PreflightError(
            "computer mTLS proxy must have exact private networks and certificates"
        )
    caddyfiles = [
        row
        for row in proxy_volumes
        if isinstance(row, dict) and row.get("target") == "/etc/caddy/Caddyfile"
    ]
    if (
        len(proxy_volumes) != 1
        or len(caddyfiles) != 1
        or caddyfiles[0].get("type") != "bind"
        or caddyfiles[0].get("read_only") is not True
    ):
        raise PreflightError("computer mTLS proxy must bind one read-only Caddyfile")
    caddyfile = Path(caddyfiles[0].get("source", ""))
    try:
        text = caddyfile.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise PreflightError("computer mTLS proxy Caddyfile is unavailable") from error
    required_lines = (
        "mode require_and_verify",
        "trust_pool file /run/secrets/exchange-plugin-client-ca",
        "reverse_proxy h2c://computer-runtime:50051",
    )
    if any(required not in text for required in required_lines):
        raise PreflightError(
            "computer proxy must require Exchange mTLS and forward only to private h2c"
        )

    _validate_computer_exchange_profile(compose)


def _memory_limit_bytes(value: Any) -> int | None:
    """Normalize Docker Compose memory limits to bytes without accepting fractions."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    if value.isdecimal():
        parsed = int(value)
        return parsed if parsed > 0 else None
    match = re.fullmatch(r"([1-9][0-9]*)([kKmMgG])", value)
    if match is None:
        return None
    amount = int(match.group(1))
    suffix = match.group(2).lower()
    return amount * {"k": 1024, "m": MIB, "g": GIB}[suffix]


def _cpu_limit_decimal(value: Any) -> Decimal | None:
    """Normalize string/numeric CPU limits for exact security-boundary comparisons."""

    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _computer_runtime_storage_report(compose: dict[str, Any]) -> dict[str, Any]:
    """Declare the configured lifetime of the optional computer runtime scratch paths."""

    if "computer-runtime" in compose.get("services", {}):
        return {
            "lifetime": "EPHEMERAL",
            "workspaceClearedOnRestart": True,
            "artifactRefsSurviveRestart": False,
        }
    return {
        "lifetime": "NOT_PRESENT",
        "workspaceClearedOnRestart": None,
        "artifactRefsSurviveRestart": None,
    }


def _validate_computer_exchange_profile(compose: dict[str, Any]) -> None:
    """Bind the private Exchange runner profile to the internal mTLS proxy and limits."""

    services = compose.get("services", {})
    exchange = services.get("exchange", {})
    environment = exchange.get("environment", {})
    try:
        configuration = json.loads(
            environment.get("CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON", ""),
            object_pairs_hook=_unique_json_object,
        )
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError(
            "Exchange computer runner integration config is invalid JSON"
        ) from error
    connector_host = configuration.get("connectorHost") if isinstance(configuration, dict) else None
    computer = connector_host.get("computerRunner") if isinstance(connector_host, dict) else None
    host_secret_refs = (
        configuration.get("hostSecretRefs") if isinstance(configuration, dict) else None
    )
    if (
        not isinstance(computer, dict)
        or set(computer)
        != {
            "profileRef",
            "connectionRef",
            "organizationId",
            "workspaceId",
            "expectedIssuer",
            "directPlugin",
            "maxCommandTimeoutMs",
            "maxFileBytes",
            "maxReadBytes",
            "maxDirectoryDepth",
        }
        or not isinstance(host_secret_refs, dict)
        or PLACEHOLDER_RE.search(json.dumps(configuration, sort_keys=True))
    ):
        raise PreflightError(
            "Exchange must configure a complete host-owned computer runner profile without placeholders"
        )
    uuid_pattern = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    tenant_id = services.get("navigator", {}).get("environment", {}).get("TEAMS_TENANT_ID")
    organization_id = (
        services.get("workspace-authority", {}).get("environment", {}).get("CYRENE_ORGANIZATION_ID")
    )
    workspace_id = (
        services.get("workspace-authority", {}).get("environment", {}).get("CYRENE_WORKSPACE_ID")
    )
    expected_issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
    if (
        computer.get("profileRef") != "computer-workspace-main"
        or not isinstance(computer.get("connectionRef"), str)
        or not re.fullmatch(uuid_pattern, computer["connectionRef"])
        or computer.get("organizationId") != organization_id
        or not isinstance(organization_id, str)
        or not re.fullmatch(uuid_pattern, organization_id)
        or computer.get("workspaceId") != workspace_id
        or not isinstance(workspace_id, str)
        or not re.fullmatch(uuid_pattern, workspace_id)
        or computer.get("expectedIssuer") != expected_issuer
        or computer.get("maxCommandTimeoutMs") != 600000
        or isinstance(computer.get("maxCommandTimeoutMs"), bool)
        or computer.get("maxFileBytes") != 1048576
        or isinstance(computer.get("maxFileBytes"), bool)
        or computer.get("maxReadBytes") != 65536
        or isinstance(computer.get("maxReadBytes"), bool)
        or computer.get("maxDirectoryDepth") != 32
        or isinstance(computer.get("maxDirectoryDepth"), bool)
    ):
        raise PreflightError(
            "Exchange computer runner profile identity or operation limits do not match the owner-approved workspace"
        )
    plugin = computer.get("directPlugin")
    if (
        not isinstance(plugin, dict)
        or set(plugin) != {"bindingId", "connectionRef", "transport", "mtls"}
        or plugin.get("bindingId") != "computer-runtime"
        or plugin.get("connectionRef") != "grpcs://computer-runtime-proxy:7443"
        or plugin.get("transport") != "mtls"
        or plugin.get("mtls")
        != {
            "rootCertificateRef": "tls/plugin-root",
            "clientCertificateRef": "tls/exchange-plugin-client-cert",
            "clientKeyRef": "tls/exchange-plugin-client-key",
        }
    ):
        raise PreflightError("Exchange computer runner must use the internal proxy with mTLS")
    secret_paths = {
        "tls/plugin-root": (
            "EXCHANGE_COMPUTER_PROXY_ROOT_CA_FILE",
            "/run/secrets/exchange-plugin-root-ca",
            "exchange-plugin-root-ca",
        ),
        "tls/exchange-plugin-client-cert": (
            "EXCHANGE_COMPUTER_PROXY_CLIENT_CERT_FILE",
            "/run/secrets/exchange-plugin-client-cert",
            "exchange-plugin-client-cert",
        ),
        "tls/exchange-plugin-client-key": (
            "EXCHANGE_COMPUTER_PROXY_CLIENT_KEY_FILE",
            "/run/secrets/exchange-plugin-client-key",
            "exchange-plugin-client-key",
        ),
    }
    mounted_secrets = _service_secret_targets(exchange)
    for secret_ref, (environment_name, path, secret_target) in secret_paths.items():
        if (
            host_secret_refs.get(secret_ref) != environment_name
            or environment.get(environment_name) != path
            or secret_target not in mounted_secrets
        ):
            raise PreflightError(
                "Exchange computer runner hostSecretRefs must resolve only to its mounted mTLS files"
            )
    if "exchange-computer-private" not in exchange.get("networks", {}):
        raise PreflightError("Exchange must use the internal computer proxy network")


def _expected_exchange_providers(compose: dict[str, Any], mode: str) -> set[str]:
    """Return the exact Exchange host-profile providers enabled in one phase."""

    providers = {"hf"} if _huggingface_enabled(compose) else set()
    if mode in {"stage1", "staging"}:
        return providers
    if mode == "stage2":
        return providers | {"azure", "gcp"}
    if mode == "stage3":
        return providers | {"azure", "gcp", "runpod"}
    services = compose.get("services", {})
    cloud_service = services.get("cloud-management-phase2")
    if isinstance(cloud_service, dict):
        providers.update({"azure", "gcp"})
    if isinstance(cloud_service, dict) and "runpod" in cloud_service.get("profiles", []):
        providers.add("runpod")
    return providers


def _validate_exchange_profile_stage(compose: dict[str, Any], mode: str) -> None:
    """Prevent Exchange from exposing later cloud profiles before their stage gate."""

    services = compose.get("services", {})
    service = services.get("exchange", {})
    environment = service.get("environment", {})
    runner_enabled = environment.get("CYRENE_EXCHANGE_AUTHORIZATION_CLOUD_RUNNER_ENABLED", "false")
    if runner_enabled not in {"true", "false"}:
        raise PreflightError(
            "Exchange AuthorizationCloudRunner must use exact lowercase true/false"
        )
    encoded = environment.get("CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON", "")
    try:
        document = json.loads(encoded, object_pairs_hook=_unique_json_object)
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError("Exchange host configuration is invalid JSON") from error
    if not isinstance(document, dict) or PLACEHOLDER_RE.search(
        json.dumps(document, sort_keys=True)
    ):
        raise PreflightError("Exchange host configuration is missing or contains placeholders")
    if "wecom" in document or "emailNotifications" in document:
        raise PreflightError("unconfigured WeCom and email connector profiles must remain absent")
    authority = document.get("platformAuthority")
    if (
        not isinstance(authority, dict)
        or "connectorWorkload" not in authority
        or "productWorkload" in authority
        or "identity" in authority
    ):
        raise PreflightError("Exchange must use the connector-only Authority workload")
    platform_workload = authority["connectorWorkload"]
    if (
        not isinstance(platform_workload, dict)
        or platform_workload.get("connectionRef") != "grpcs://workspace-authority:7443"
    ):
        raise PreflightError(
            "Exchange connector workload must target the private Authority service"
        )
    account = document.get("accountCenter")
    github = document.get("githubIssues")
    connector_host = document.get("connectorHost")
    teams = connector_host.get("teamsMessages") if isinstance(connector_host, dict) else None
    if (
        not isinstance(account, dict)
        or account.get("connectionRef") != "grpcs://account-center:7443"
        or account.get("transport") != "mtls"
        or account.get("callerId") != "exchange-service"
        or account.get("signingKeyRef") != "account/signing"
        or not isinstance(github, dict)
        or github.get("connectionRef") != "grpcs://github-issues:7443"
        or github.get("transport") != "mtls"
        or github.get("callerId") != "exchange-service"
        or github.get("signingKeyRef") != "account/signing"
        or not isinstance(teams, dict)
        or not isinstance(teams.get("directPlugin"), dict)
        or teams["directPlugin"].get("connectionRef") != "grpcs://teams-provider:7443"
        or teams["directPlugin"].get("transport") != "mtls"
    ):
        raise PreflightError(
            "Exchange core Account Center/GitHub/Teams bindings do not match private mTLS services"
        )
    expected_providers = _expected_exchange_providers(compose, mode)
    if not expected_providers:
        if "authorizationCloud" in document:
            raise PreflightError(
                "Exchange authorizationCloud must be omitted when no cloud provider profiles are expected"
            )
        if runner_enabled == "true":
            raise PreflightError("AuthorizationCloudRunner cannot run without cloud profiles")
        return

    cloud = document.get("authorizationCloud")
    profiles = cloud.get("profiles") if isinstance(cloud, dict) else None
    if not isinstance(profiles, dict):
        raise PreflightError("Exchange requires the phase-scoped cloud provider profile map")
    if runner_enabled == "true" and cloud.get("monitorIntervalSeconds") != 900:
        raise PreflightError("AuthorizationCloudRunner must poll at the fixed 900-second interval")
    actual_providers = {
        profile.get("provider") for profile in profiles.values() if isinstance(profile, dict)
    }
    if actual_providers != expected_providers:
        raise PreflightError(
            f"Exchange cloud profiles must be exactly {sorted(expected_providers)} for this stage"
        )
    for provider, profile in (
        (profile.get("provider"), profile)
        for profile in profiles.values()
        if isinstance(profile, dict)
    ):
        endpoint = profile.get("directPlugin")
        expected_target = (
            "grpcs://huggingface:7443" if provider == "hf" else "grpcs://cloud-management:7443"
        )
        if (
            not isinstance(endpoint, dict)
            or endpoint.get("connectionRef") != expected_target
            or endpoint.get("transport") != "mtls"
        ):
            raise PreflightError(
                f"Exchange {provider} profile must target its private mTLS Provider service"
            )


def _verify_provider_profiles(encoded: Any, required: set[str]) -> None:
    """Check the externally supplied cloud profile JSON before starting the provider."""

    if not isinstance(encoded, str):
        raise PreflightError("cloud provider profile configuration is missing")
    try:
        config = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise PreflightError("cloud provider profile configuration is invalid JSON") from error
    profiles = config.get("profiles") if isinstance(config, dict) else None
    if not isinstance(profiles, list):
        raise PreflightError("cloud provider configuration has no profile list")
    actual = {row.get("provider") for row in profiles if isinstance(row, dict)}
    if actual != required:
        raise PreflightError(
            f"cloud provider phase must contain exactly {sorted(required)} profiles"
        )
    serialized = json.dumps(config, sort_keys=True)
    if PLACEHOLDER_RE.search(serialized):
        raise PreflightError("cloud provider profile configuration contains placeholders")


def _verify_azure_cli_scope(service: dict[str, Any]) -> None:
    """Bind the Azure CLI runner to the same UAMI and subscription as the provider profile."""

    environment = service.get("environment", {})
    try:
        config = json.loads(
            environment.get("CYRENE_CAPABILITY_CONFIGURATION_JSON", ""),
            object_pairs_hook=_unique_json_object,
        )
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError("cloud provider profile configuration is invalid JSON") from error
    profiles = config.get("profiles") if isinstance(config, dict) else None
    azure_profiles = (
        [row for row in profiles if isinstance(row, dict) and row.get("provider") == "azure"]
        if isinstance(profiles, list)
        else []
    )
    if len(azure_profiles) != 1:
        raise PreflightError("cloud acceptance requires exactly one scoped Azure profile")
    profile = azure_profiles[0]
    scope = profile.get("scope")
    auth = profile.get("auth")
    client_id = environment.get("AZURE_CLIENT_ID")
    subscription_id = environment.get("CYRENE_CLOUD_MGMT_AZURE_SUBSCRIPTION_ID")
    if (
        not isinstance(scope, dict)
        or not isinstance(auth, dict)
        or scope.get("subscription_id") != subscription_id
        or auth.get("mode") != "managed_identity"
        or auth.get("client_id") != client_id
    ):
        raise PreflightError(
            "Azure CLI managed identity/subscription must match the approved Cloud Management profile"
        )


def _verify_uami(client_id: Any) -> None:
    """Require a dedicated User Assigned Managed Identity client ID."""

    if not isinstance(client_id, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", client_id
    ):
        raise PreflightError(
            "Azure cloud management requires the dedicated User Assigned Managed Identity client ID"
        )


def _validate_account_center_config(
    service: dict[str, Any], *, huggingface_enabled: bool = True
) -> None:
    """Keep GitHub credentials host-managed and make HF references explicitly optional."""

    environment = service.get("environment", {})
    encoded = environment.get("CYRENE_CAPABILITY_CONFIGURATION_JSON", "")
    try:
        config = json.loads(encoded, object_pairs_hook=_unique_json_object)
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError("Account Center activation config is invalid JSON") from error
    if not isinstance(config, dict) or PLACEHOLDER_RE.search(json.dumps(config, sort_keys=True)):
        raise PreflightError("Account Center activation config is missing or contains placeholders")
    refs = config.get("hostSecretRefs")
    if not isinstance(refs, dict) or not refs:
        raise PreflightError("Account Center must resolve every credential through hostSecretRefs")
    if config.get("github_app_private_key_ref") != "github-app/private-key":
        raise PreflightError(
            "Account Center must use the dedicated GitHub App private-key reference"
        )
    provider_file_refs = config.get("provider_secret_file_refs", [])
    file_refs = config.get("hostSecretFileRefs", {})
    provider_secret_refs = config.get("provider_secret_refs", [])
    if (
        not isinstance(provider_file_refs, list)
        or not isinstance(file_refs, dict)
        or not isinstance(provider_secret_refs, list)
    ):
        raise PreflightError("Account Center provider secret references are invalid")
    if huggingface_enabled:
        if provider_file_refs != ["huggingface/connector-token"] or file_refs != {
            "huggingface/connector-token": "/run/secrets/huggingface-oauth.json"
        }:
            raise PreflightError(
                "HF-enabled Account Center must resolve its real OAuth seed from the private secret file"
            )
    else:
        huggingface_refs = any(
            "huggingface" in str(reference).lower()
            or "huggingface" in str(env_name).lower()
            or "hf_provider" in str(env_name).lower()
            for reference, env_name in refs.items()
        )
        if (
            provider_file_refs
            or file_refs
            or provider_secret_refs
            or huggingface_refs
            or config.get("huggingface_provider_caller_id") is not None
        ):
            raise PreflightError(
                "GitHub-only Account Center configuration must contain no Hugging Face refs or profiles"
            )
    if provider_secret_refs or any(
        reference in refs
        for reference in (
            "huggingface/connector-token",
            "huggingface/fine-grained-token",
        )
    ):
        raise PreflightError(
            "Hugging Face OAuth seed must not be copied into the Account Center environment"
        )
    if config.get("encryption_key_ref") != "account-center/encryption-key":
        raise PreflightError("Account Center must use a private binding encryption-key reference")
    expected = {
        "github-app/private-key": "CYRENE_ACCOUNT_CENTER_GITHUB_APP_PRIVATE_KEY",
        "account-center/encryption-key": "CYRENE_ACCOUNT_CENTER_ENCRYPTION_KEY",
    }
    for ref, env_name in expected.items():
        if refs.get(ref) != env_name:
            raise PreflightError(f"Account Center hostSecretRef mapping is missing for {ref}")
    mappings = set(environment.get("CYRENE_SECRET_FILE_MAP", "").split(","))
    mapped_names = {part.split("=", 1)[0] for part in mappings if "=" in part}
    if any(name not in mapped_names for name in refs.values()):
        raise PreflightError("Account Center hostSecretRefs must resolve to mounted secret files")
    if huggingface_enabled:
        if not isinstance(file_refs, dict) or set(file_refs.values()) != {
            "/run/secrets/huggingface-oauth.json"
        }:
            raise PreflightError(
                "Account Center Hugging Face file reference must use its private mount"
            )
    else:
        mounted_hf_secrets = {
            row.get("target") for row in service.get("secrets", []) if isinstance(row, dict)
        } & {"account-center-hf-provider-hmac-key", "huggingface-oauth.json"}
        map_values = environment.get("CYRENE_SECRET_FILE_MAP", "").lower()
        networks = service.get("networks", {})
        if (
            mounted_hf_secrets
            or "hf_provider" in map_values
            or any("huggingface" in str(name).lower() for name in networks)
        ):
            raise PreflightError(
                "GitHub-only Account Center must not mount Hugging Face secrets or networks"
            )


def _connector_bootstrap_actor(config: dict[str, Any]) -> dict[str, Any]:
    """Derive the bootstrap actor from the same Directory/Teams owner scope as preflight."""

    services = config.get("services", {})
    navigator = services.get("navigator", {}).get("environment", {})
    authority = services.get("workspace-authority", {}).get("environment", {})
    operator = services.get("directory-operator", {}).get("environment", {})
    tenant_id = navigator.get("TEAMS_TENANT_ID")
    return {
        "issuer": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        "subject": operator.get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID"),
        "organizationId": authority.get("CYRENE_ORGANIZATION_ID"),
        "workspaceId": authority.get("CYRENE_WORKSPACE_ID"),
    }


def _connector_bootstrap_cloud_profiles(
    config: dict[str, Any], mode: str
) -> dict[str, dict[str, Any]]:
    """Join Exchange bootstrap selectors to the exact candidate Cloud Management profiles."""

    if mode not in {"connector-auth-readonly", "stage2", "stage3", "cutover"}:
        return {}
    services = config.get("services", {})
    cloud_service = services.get("cloud-management-phase2") if isinstance(services, dict) else None
    exchange_service = services.get("exchange") if isinstance(services, dict) else None
    if mode == "connector-auth-readonly" and (
        not isinstance(cloud_service, dict) or not isinstance(exchange_service, dict)
    ):
        raise PreflightError(
            "connector bootstrap cloud profiles require Cloud Management and Exchange"
        )
    if not isinstance(cloud_service, dict):
        return {}
    if not isinstance(exchange_service, dict):
        raise PreflightError("Exchange cloud bootstrap configuration is unavailable")
    cloud_document = _json_environment_object(
        cloud_service, "CYRENE_CAPABILITY_CONFIGURATION_JSON", "Cloud Management"
    )
    cloud_rows = cloud_document.get("profiles")
    if not isinstance(cloud_rows, list):
        raise PreflightError("Cloud Management bootstrap profile list is invalid")
    provider_mode = "cutover" if mode == "connector-auth-readonly" else mode
    expected_providers = _expected_exchange_providers(config, provider_mode) - (
        {"hf"} if _huggingface_enabled(config) else set()
    )
    rows_by_provider: dict[str, dict[str, Any]] = {}
    for row in cloud_rows:
        if not isinstance(row, dict):
            raise PreflightError("Cloud Management bootstrap profile list is invalid")
        provider = row.get("provider")
        profile_ref = row.get("profile_ref")
        identity_ref = row.get("identity_ref")
        if (
            not isinstance(provider, str)
            or not isinstance(profile_ref, str)
            or not profile_ref
            or not isinstance(identity_ref, str)
            or not identity_ref
            or provider in rows_by_provider
        ):
            raise PreflightError("Cloud Management bootstrap profile identity is invalid")
        rows_by_provider[provider] = row
    if set(rows_by_provider) != expected_providers or expected_providers not in (
        {"azure", "gcp"},
        {"azure", "gcp", "runpod"},
    ):
        raise PreflightError("connector bootstrap cloud providers must match the candidate phase")

    exchange_document = _json_environment_object(
        exchange_service,
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON",
        "Exchange",
    )
    authorization_cloud = exchange_document.get("authorizationCloud")
    exchange_profiles = (
        authorization_cloud.get("profiles") if isinstance(authorization_cloud, dict) else None
    )
    if not isinstance(exchange_profiles, dict):
        raise PreflightError("Exchange bootstrap cloud profile map is unavailable")

    result: dict[str, dict[str, Any]] = {}
    for provider, cloud_row in rows_by_provider.items():
        profile_ref = cloud_row["profile_ref"]
        identity_ref = cloud_row["identity_ref"]
        exchange_profile = exchange_profiles.get(profile_ref)
        targets = exchange_profile.get("targets") if isinstance(exchange_profile, dict) else None
        if (
            not isinstance(exchange_profile, dict)
            or exchange_profile.get("provider") != provider
            or exchange_profile.get("identityRef") != identity_ref
            or exchange_profile.get("accessMode") != "workspace_operator"
            or not isinstance(targets, dict)
            or not targets
        ):
            raise PreflightError(
                "Exchange cloud bootstrap profile must join to the candidate provider identity and targets"
            )
        for target_alias, target in targets.items():
            if (
                not isinstance(target_alias, str)
                or not target_alias
                or not isinstance(target, dict)
                or set(target) - {"targetRef", "displayName", "allowedContextRefs"}
                or not {"targetRef", "displayName"} <= set(target)
                or any(
                    not isinstance(target.get(field), str) or not target[field]
                    for field in ("targetRef", "displayName")
                )
                or (
                    "allowedContextRefs" in target
                    and (
                        not isinstance(target["allowedContextRefs"], list)
                        or any(
                            not isinstance(value, str) or not value
                            for value in target["allowedContextRefs"]
                        )
                        or len(target["allowedContextRefs"])
                        != len(set(target["allowedContextRefs"]))
                    )
                )
            ):
                raise PreflightError("Exchange cloud bootstrap target configuration is invalid")
        result[provider] = {
            "profileAlias": profile_ref,
            "targets": targets,
        }
    return result


def _validate_connector_bootstrap_config(
    path: Path,
    *,
    huggingface_enabled: bool,
    expected_actor: dict[str, Any],
    expected_cloud_profiles: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Require the private Exchange bootstrap input to match the selected provider set."""

    try:
        document = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError("private connector bootstrap configuration is invalid JSON") from error
    required = {"schemaVersion", "actor", "github"}
    expected_cloud_profiles = expected_cloud_profiles or {}
    if (
        not isinstance(document, dict)
        or not required <= set(document)
        or set(document) - (required | {"huggingface", "cloud"})
        or document.get("schemaVersion") != 1
        or not isinstance(document.get("actor"), dict)
        or set(document["actor"]) != {"issuer", "subject", "organizationId", "workspaceId"}
        or document["actor"] != expected_actor
        or PLACEHOLDER_RE.search(json.dumps(document, sort_keys=True))
        or ("huggingface" in document) != huggingface_enabled
    ):
        raise PreflightError(
            "private connector bootstrap actor and provider set must match the approved Directory owner and CYRENE_HF_ENABLED"
        )
    github_bootstrap = document.get("github")
    if (
        not isinstance(github_bootstrap, dict)
        or github_bootstrap.get("expectedAccessMode") != "workspace_operator"
    ):
        raise PreflightError("GitHub connector bootstrap must require workspace_operator access")
    if huggingface_enabled:
        huggingface_bootstrap = document.get("huggingface")
        if (
            not isinstance(huggingface_bootstrap, dict)
            or huggingface_bootstrap.get("expectedAccessMode") != "workspace_operator"
        ):
            raise PreflightError(
                "Hugging Face connector bootstrap must require workspace_operator access"
            )
    cloud_entries = document.get("cloud")
    if expected_cloud_profiles:
        if not isinstance(cloud_entries, list) or len(cloud_entries) != len(
            expected_cloud_profiles
        ):
            raise PreflightError(
                "private connector bootstrap cloud provider set must match the candidate phase"
            )
        seen_providers: set[str] = set()
        for entry in cloud_entries:
            if not isinstance(entry, dict) or set(entry) != {
                "profileAlias",
                "targetAlias",
                "expectedAccessMode",
            }:
                raise PreflightError(
                    "cloud bootstrap entries must match the Exchange v1 closed schema"
                )
            profile_alias = entry.get("profileAlias")
            target_alias = entry.get("targetAlias")
            if (
                not isinstance(profile_alias, str)
                or not isinstance(target_alias, str)
                or not profile_alias
                or not target_alias
                or entry.get("expectedAccessMode") != "workspace_operator"
            ):
                raise PreflightError(
                    "cloud bootstrap entries require profile/target aliases and workspace_operator access"
                )
            matching = [
                provider
                for provider, expected in expected_cloud_profiles.items()
                if expected.get("profileAlias") == profile_alias
            ]
            if len(matching) != 1:
                raise PreflightError(
                    "cloud bootstrap profileAlias must select one candidate phase provider"
                )
            provider = matching[0]
            targets = expected_cloud_profiles[provider].get("targets")
            if (
                provider in seen_providers
                or not isinstance(targets, dict)
                or target_alias not in targets
            ):
                raise PreflightError(
                    "cloud bootstrap targetAlias must exist on its exact Exchange provider profile"
                )
            seen_providers.add(provider)
        if seen_providers != set(expected_cloud_profiles):
            raise PreflightError(
                "private connector bootstrap cloud provider set must match the candidate phase"
            )
    elif "cloud" in document:
        raise PreflightError("cloud bootstrap profiles are not allowed before the provider phase")


def _validate_teams_connector_config(service: dict[str, Any], navigator: dict[str, Any]) -> None:
    """Pin the Teams provider secret reference to the separately mounted bot secret."""

    environment = service.get("environment", {})
    try:
        config = json.loads(environment.get("CYRENE_CAPABILITY_CONFIGURATION_JSON", ""))
    except (TypeError, json.JSONDecodeError) as error:
        raise PreflightError("Teams provider activation config is invalid JSON") from error
    if not isinstance(config, dict) or PLACEHOLDER_RE.search(json.dumps(config, sort_keys=True)):
        raise PreflightError("Teams provider activation config is missing or contains placeholders")
    allowed_fields = {
        "binding_id",
        "bot_app_id",
        "tenant_id",
        "allowed_team_ids",
        "client_secret_env_ref",
        "service_url",
        "timeout_seconds",
    }
    navigator_environment = navigator.get("environment", {})
    team_ids = navigator_environment.get("TEAMS_ALLOWED_TEAM_IDS", "").split(",")
    if (
        set(config) - allowed_fields
        or config.get("binding_id") not in {None, environment.get("CYRENE_CAPABILITY_BINDING_ID")}
        or config.get("bot_app_id") != navigator_environment.get("TEAMS_BOT_APP_ID")
        or config.get("tenant_id") != navigator_environment.get("TEAMS_TENANT_ID")
        or config.get("allowed_team_ids") != team_ids
        or config.get("client_secret_env_ref") != "TEAMS_BOT_CLIENT_SECRET"
    ):
        raise PreflightError(
            "Teams provider activation does not match the admitted bot/team and secret reference"
        )
    mappings = dict(
        part.split("=", 1)
        for part in environment.get("CYRENE_SECRET_FILE_MAP", "").split(",")
        if "=" in part
    )
    mounted = {row.get("source") for row in service.get("secrets", []) if isinstance(row, dict)}
    if (
        mappings.get("TEAMS_BOT_CLIENT_SECRET") != "/run/secrets/teams-bot-client-secret"
        or "teams-bot-client-secret" not in mounted
    ):
        raise PreflightError(
            "Teams provider bot client secret must be mounted only through its host file reference"
        )


def _validate_teams_delivery_spool(config: dict[str, Any]) -> None:
    """Require durable ingress storage and a separate read-only admin secret.

    The host bind must already be provisioned for UID 10001. The token itself is
    checked by metadata only here; runtime admin requests read it inside ingress.
    中文：spool 使用 UID 10001 的持久宿主机目录；预检不读取管理 token 内容。
    """

    services = config.get("services", {})
    service = services.get("teams-ingress") if isinstance(services, dict) else None
    if service is None:
        return
    if not isinstance(service, dict):
        raise PreflightError("Teams ingress delivery configuration is malformed")
    environment = service.get("environment", {})
    volumes = service.get("volumes", [])
    secrets = service.get("secrets", [])
    state_mounts = [
        row
        for row in volumes
        if isinstance(row, dict) and row.get("target") == "/var/lib/cyrene/teams-delivery"
    ]
    admin_secret_mounts = [
        row
        for row in secrets
        if isinstance(row, dict) and row.get("source") == "teams-delivery-admin-token"
    ]
    if (
        environment.get("TEAMS_NAVIGATOR_DELIVERY_STATE_DIR") != "/var/lib/cyrene/teams-delivery"
        or environment.get("TEAMS_DELIVERY_ADMIN_TOKEN_FILE")
        != "/run/secrets/teams-delivery-admin-token"
        or "TEAMS_DELIVERY_ADMIN_TOKEN" in environment
        or len(state_mounts) != 1
        or state_mounts[0].get("type") != "bind"
        or state_mounts[0].get("read_only", False) is not False
        or len(admin_secret_mounts) != 1
        or admin_secret_mounts[0].get("target") != "teams-delivery-admin-token"
        or service.get("ports")
    ):
        raise PreflightError(
            "Teams delivery spool must use its fixed persistent path and private admin-secret mount"
        )
    for name, candidate in services.items():
        if name == "teams-ingress" or not isinstance(candidate, dict):
            continue
        if any(
            isinstance(row, dict) and row.get("source") == "teams-delivery-admin-token"
            for row in candidate.get("secrets", [])
        ):
            raise PreflightError(
                "Teams delivery admin token may be mounted only into Teams ingress"
            )

    source = state_mounts[0].get("source")
    if not isinstance(source, str) or not source:
        raise PreflightError("Teams delivery state bind source is missing")
    _validate_teams_delivery_state_directory(Path(source))


def _validate_credential_notification_activation(
    config: dict[str, Any],
    *,
    config_dir: Path,
    anchor_file: Path | None,
    provenance_file: Path | None,
    expected_provider_ids: set[str],
) -> dict[str, str]:
    """Bind the owner-only notification DTO to the real anchor and Exchange system profile."""

    services = config.get("services", {})
    navigator = services.get("navigator", {})
    environment = navigator.get("environment", {}) if isinstance(navigator, dict) else {}
    if environment.get("TEAMS_CREDENTIAL_NOTIFICATIONS_CONFIG_FILE") != (
        "/run/secrets/teams-credential-notifications.json"
    ):
        raise PreflightError("Navigator credential notification config path is not fixed")
    mounts = [
        row
        for row in navigator.get("configs", [])
        if isinstance(row, dict)
        and row.get("source") == "teams-credential-notifications"
        and row.get("target") == "/run/secrets/teams-credential-notifications.json"
    ]
    resolved = config.get("configs", {}).get("teams-credential-notifications", {})
    source_text = resolved.get("file") if isinstance(resolved, dict) else None
    if len(mounts) != 1 or not isinstance(source_text, str):
        raise PreflightError("credential notification config must use its one fixed Compose mount")
    config_path = Path(source_text)
    if config_path.resolve(strict=True) != (
        config_dir / "teams-credential-notifications.json"
    ).resolve(strict=True):
        raise PreflightError(
            "credential notification config must stay in the private config directory"
        )
    if anchor_file is None or anchor_file.is_symlink() or not anchor_file.is_file():
        raise PreflightError("a separate private owner/destination notification anchor is required")
    if provenance_file is None or provenance_file.is_symlink() or not provenance_file.is_file():
        raise PreflightError("a private credential notification provenance receipt is required")
    if not _private_artifact(anchor_file) or not _private_artifact(provenance_file):
        raise PreflightError(
            "credential notification anchor and provenance must stay under private umbrella artifacts"
        )
    for path, label in (
        (config_path, "config"),
        (anchor_file, "anchor"),
        (provenance_file, "provenance"),
    ):
        metadata = path.stat()
        if (
            metadata.st_uid != 10001
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or path.is_symlink()
            or not path.is_file()
        ):
            raise PreflightError(
                f"credential notification {label} must be owner-only UID 10001 mode 0600"
            )
    try:
        document = json.loads(
            config_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
        provenance = json.loads(
            provenance_file.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError(
            "credential notification config or provenance is invalid JSON"
        ) from error
    expected_fields = {
        "schemaVersion",
        "organizationId",
        "workspaceId",
        "owner",
        "connections",
        "destination",
        "exchange",
    }
    optional_fields = {"pollIntervalMs"}
    if (
        not isinstance(document, dict)
        or set(document) - expected_fields - optional_fields
        or not expected_fields <= set(document)
        or document.get("schemaVersion") != 1
        or isinstance(document.get("schemaVersion"), bool)
        or (
            "pollIntervalMs" in document
            and (
                isinstance(document["pollIntervalMs"], bool)
                or not isinstance(document["pollIntervalMs"], int)
                or not 60_000 <= document["pollIntervalMs"] <= 900_000
            )
        )
    ):
        raise PreflightError("credential notification document has an unsupported closed schema")
    operator = services.get("directory-operator", {}).get("environment", {})
    authority = services.get("workspace-authority", {}).get("environment", {})
    tenant = services.get("navigator", {}).get("environment", {}).get("TEAMS_TENANT_ID")
    team_ids = (
        services.get("navigator", {})
        .get("environment", {})
        .get("TEAMS_ALLOWED_TEAM_IDS", "")
        .split(",")
    )
    expected_owner = {
        "actorAadObjectId": operator.get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID"),
        "issuer": f"https://login.microsoftonline.com/{tenant}/v2.0",
        "subject": operator.get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID"),
        "tenantId": tenant,
    }
    if (
        document.get("organizationId") != authority.get("CYRENE_ORGANIZATION_ID")
        or document.get("workspaceId") != authority.get("CYRENE_WORKSPACE_ID")
        or document.get("owner") != expected_owner
    ):
        raise PreflightError(
            "credential notification owner scope differs from Directory/Teams identity"
        )
    connections = document.get("connections")
    if not isinstance(connections, list) or len(connections) != len(expected_provider_ids):
        raise PreflightError(
            "credential notification config must pin exactly the configured issued provider connections"
        )
    short_names = {"huggingface": "hf", "azure": "azure", "gcp": "gcp", "runpod": "runpod"}
    if not expected_provider_ids <= set(short_names):
        raise PreflightError("credential notification provider set is unsupported")
    provider_map = {short_names[provider]: provider for provider in sorted(expected_provider_ids)}
    expected_binding_ids: dict[str, str] = {}
    try:
        binding_document = json.loads(
            (config_dir / "connector-bindings.json").read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError(
            "issued connector bindings are unavailable for notification validation"
        ) from error
    for binding in (
        binding_document.get("bindings", []) if isinstance(binding_document, dict) else []
    ):
        if isinstance(binding, dict) and binding.get("providerId") in expected_provider_ids:
            provider_id = str(binding["providerId"])
            if provider_id in expected_binding_ids:
                raise PreflightError(
                    f"credential notification binding set contains multiple {provider_id} connections"
                )
            expected_binding_ids[provider_id] = str(binding.get("connectionRef", ""))
    if set(expected_binding_ids) != expected_provider_ids:
        raise PreflightError(
            "credential notification binding set differs from configured providers"
        )
    observed_connections: dict[str, str] = {}
    uuid_pattern = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    for row in connections:
        if not isinstance(row, dict) or set(row) != {"connectionId", "provider"}:
            raise PreflightError("credential notification connection has an unsupported shape")
        provider = row.get("provider")
        connection_id = row.get("connectionId")
        if (
            provider not in provider_map
            or provider in observed_connections
            or not isinstance(connection_id, str)
            or re.fullmatch(uuid_pattern, connection_id) is None
        ):
            raise PreflightError(
                "credential notification connection is not an issued provider UUID"
            )
        observed_connections[provider] = connection_id
    expected_connection_ids = {
        short_name: expected_binding_ids.get(provider_id)
        for short_name, provider_id in provider_map.items()
    }
    if observed_connections != expected_connection_ids:
        raise PreflightError(
            "credential notification IDs differ from the issued connector bindings"
        )
    destination = document.get("destination")
    if not isinstance(destination, dict) or set(destination) != {
        "tenantId",
        "teamId",
        "channelId",
        "conversationId",
        "rootThreadId",
        "serviceUrl",
    }:
        raise PreflightError("credential notification destination has an unsupported shape")
    if (
        destination.get("tenantId") != tenant
        or destination.get("teamId") not in team_ids
        or not all(
            isinstance(destination.get(name), str)
            and 1 <= len(destination[name]) <= maximum
            and destination[name].isprintable()
            for name, maximum in (
                ("teamId", 256),
                ("channelId", 256),
                ("conversationId", 512),
                ("rootThreadId", 512),
            )
        )
        or not destination["rootThreadId"].isascii()
        or not destination["rootThreadId"].isdecimal()
        or int(destination["rootThreadId"]) == 0
    ):
        raise PreflightError(
            "credential notification destination differs from the admitted Teams scope"
        )
    service_url = destination.get("serviceUrl")
    try:
        parsed_service_url = urlsplit(service_url)
        parsed_port = parsed_service_url.port
    except (TypeError, ValueError) as error:
        raise PreflightError("credential notification service URL is malformed") from error
    if (
        parsed_service_url.scheme != "https"
        or parsed_service_url.username is not None
        or parsed_service_url.password is not None
        or parsed_port not in {None, 443}
        or parsed_service_url.query
        or parsed_service_url.fragment
        or parsed_service_url.hostname
        not in {"smba.trafficmanager.net", "smba.infra.teams.microsoft.com"}
        or not parsed_service_url.path.startswith("/")
        or not parsed_service_url.path
        or len(service_url) > 512
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in service_url)
        or "?" in service_url
        or "#" in service_url
        or "%" in service_url
        or "\\" in service_url
        or "//" in parsed_service_url.path
    ):
        raise PreflightError(
            "credential notification service URL is outside the official Teams endpoint"
        )
    service_path_parts = [part for part in parsed_service_url.path.split("/") if part]
    regional_path = (
        len(service_path_parts) == 1 and service_path_parts[0] in {"amer", "in", "emea", "apac"}
    ) or (
        len(service_path_parts) == 2
        and service_path_parts[0] in {"amer", "in", "emea", "apac"}
        and service_path_parts[1].lower() == tenant.lower()
        and re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            service_path_parts[1],
        )
        is not None
    )
    if (
        parsed_service_url.hostname == "smba.infra.teams.microsoft.com"
        and service_path_parts != ["teams"]
    ) or (
        parsed_service_url.hostname == "smba.trafficmanager.net"
        and service_path_parts != ["teams"]
        and not regional_path
    ):
        raise PreflightError("credential notification Teams route is outside its tenant region")
    exchange = document.get("exchange")
    if (
        not isinstance(exchange, dict)
        or set(exchange) != {"profileRef"}
        or not isinstance(exchange.get("profileRef"), str)
        or not exchange["profileRef"]
        or len(exchange["profileRef"]) > 128
        or not exchange["profileRef"].isprintable()
        or PLACEHOLDER_RE.search(exchange["profileRef"])
    ):
        raise PreflightError("notification Exchange profile must come from its frozen provenance")

    exchange_service = services.get("exchange", {})
    exchange_document = _json_environment_object(
        exchange_service,
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON",
        "Exchange integration",
    )
    connector_host = exchange_document.get("connectorHost")
    teams_profile = (
        connector_host.get("teamsMessages") if isinstance(connector_host, dict) else None
    )
    system_notification = (
        teams_profile.get("systemNotification") if isinstance(teams_profile, dict) else None
    )
    if (
        not isinstance(teams_profile, dict)
        or teams_profile.get("connectionRef")
        != services.get("navigator", {}).get("environment", {}).get("TEAMS_EXCHANGE_CONNECTION_REF")
        or not isinstance(system_notification, dict)
        or set(system_notification) != {"profileRef", "ownerSubject", "fixedRoute"}
        or system_notification.get("profileRef") != exchange["profileRef"]
        or system_notification.get("ownerSubject") != expected_owner["subject"]
        or system_notification.get("fixedRoute") != destination
    ):
        raise PreflightError(
            "Exchange systemNotification profile must bind the exact owner and fixed Teams destination"
        )

    deployment_config = Path(__file__).resolve().parent / "config"
    notification_schema = deployment_config / "teams-credential-notifications.schema.json"
    exchange_profile_schema = (
        deployment_config / "teams-credential-notifications-exchange-profile.schema.json"
    )
    notification_template = deployment_config / "teams-credential-notifications.template.json"
    exchange_profile_template = (
        deployment_config / "teams-credential-notifications-exchange-profile.template.json"
    )
    source_files = (
        notification_schema,
        exchange_profile_schema,
        notification_template,
        exchange_profile_template,
    )
    if any(path.is_symlink() or not path.is_file() for path in source_files):
        raise PreflightError("credential notification schemas and templates are unavailable")

    candidate_config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()
    exchange_config_sha256 = _canonical_json_digest(exchange_document)
    anchor_sha256 = hashlib.sha256(anchor_file.read_bytes()).hexdigest()
    owner_scope_sha256 = _canonical_json_digest(
        {
            "organizationId": document["organizationId"],
            "workspaceId": document["workspaceId"],
            "owner": document["owner"],
        }
    )
    destination_sha256 = _canonical_json_digest(destination)
    schema_sha256 = hashlib.sha256(notification_schema.read_bytes()).hexdigest()
    receipt_fields = {
        "schemaVersion",
        "sourceAnchorSha256",
        "sourceTaskId",
        "candidateConfigSha256",
        "candidateExchangeHostConfigSha256",
        "schemaSha256",
        "ownerScopeSha256",
        "destinationSha256",
        "exchangeConnectionRef",
        "exchangeProfileRef",
        "sourceWorkspaceCommit",
        "generatedAt",
    }
    if (
        not isinstance(provenance, dict)
        or set(provenance) != receipt_fields
        or provenance.get("schemaVersion") != "cyrene-teams-credential-notifications-provenance-v1"
        or provenance.get("sourceAnchorSha256") != anchor_sha256
        or provenance.get("candidateConfigSha256") != candidate_config_sha256
        or provenance.get("candidateExchangeHostConfigSha256") != exchange_config_sha256
        or provenance.get("schemaSha256") != schema_sha256
        or provenance.get("ownerScopeSha256") != owner_scope_sha256
        or provenance.get("destinationSha256") != destination_sha256
        or provenance.get("exchangeConnectionRef") != teams_profile.get("connectionRef")
        or provenance.get("exchangeProfileRef") != exchange["profileRef"]
        or not isinstance(provenance.get("sourceTaskId"), str)
        or not provenance["sourceTaskId"]
        or re.fullmatch(r"[0-9a-f]{40,64}", provenance.get("sourceWorkspaceCommit", "")) is None
    ):
        raise PreflightError(
            "credential notification provenance is stale or differs from frozen host anchors"
        )
    _read_timestamp(provenance.get("generatedAt"), "credential notification provenance")
    return {
        "credentialNotificationConfigSha256": candidate_config_sha256,
        "credentialNotificationAnchorSha256": anchor_sha256,
        "credentialNotificationProvenanceSha256": hashlib.sha256(
            provenance_file.read_bytes()
        ).hexdigest(),
        "credentialNotificationExchangeConfigSha256": exchange_config_sha256,
        "credentialNotificationSchemaSha256": schema_sha256,
        "credentialNotificationExchangeProfileSchemaSha256": hashlib.sha256(
            exchange_profile_schema.read_bytes()
        ).hexdigest(),
        "credentialNotificationTemplateSha256": hashlib.sha256(
            notification_template.read_bytes()
        ).hexdigest(),
        "credentialNotificationExchangeProfileTemplateSha256": hashlib.sha256(
            exchange_profile_template.read_bytes()
        ).hexdigest(),
    }


def _validate_teams_delivery_state_directory(state_dir: Path) -> None:
    """Require the spool bind source to be private, persistent, and owned by ingress."""

    if not state_dir.is_absolute() or state_dir.is_symlink():
        raise PreflightError("Teams delivery state path must be an absolute non-symlink directory")
    try:
        resolved = state_dir.resolve(strict=True)
        metadata = state_dir.lstat()
    except OSError as error:
        raise PreflightError("Teams delivery state directory is unavailable") from error
    if (
        resolved != state_dir
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 10001
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise PreflightError(
            "Teams delivery state directory must be a real UID 10001 directory with mode 0700"
        )


def _validate_caddyfile(
    service: dict[str, Any], mode: str, *, ingress_exposure: str = "public-cutover"
) -> None:
    """Require the exact callback-only Caddy route and stage-appropriate TLS file."""

    matches = [
        row
        for row in service.get("volumes", [])
        if isinstance(row, dict) and row.get("target") == "/etc/caddy/Caddyfile"
    ]
    if len(matches) != 1 or matches[0].get("type") != "bind":
        raise PreflightError("public ingress must bind exactly one repository Caddyfile")
    path = Path(matches[0].get("source", ""))
    host_fronted_staging = (
        mode == "connector-auth-readonly" and ingress_exposure == "host-fronted-staging"
    )
    expected_name = (
        "Caddyfile.staging"
        if mode in {"bootstrap", "staging", "stage1", "stage2", "stage3"} or host_fronted_staging
        else "Caddyfile"
    )
    if path.name != expected_name or path.is_symlink() or not path.is_file():
        raise PreflightError(f"{mode} ingress must use the canonical {expected_name}")
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise PreflightError("public ingress Caddyfile could not be read") from error
    lines = [line.strip() for line in contents.splitlines() if line.strip()]
    paths = [line for line in lines if line.startswith("path ")]
    proxies = [line for line in lines if line.startswith("reverse_proxy ")]
    if paths != ["path /api/messages"] or proxies != [
        "reverse_proxy @teams_callback teams-ingress:3978"
    ]:
        raise PreflightError("Caddy may proxy only Teams POST /api/messages to the ingress sidecar")
    if (
        "method POST" not in lines
        or 'respond @wrong_method "method not allowed" 405' not in lines
        or 'respond "not found" 404' not in lines
    ):
        raise PreflightError("Caddy callback must reject other methods and paths")
    if mode in {"cutover", "connector-auth-readonly"} and not host_fronted_staging:
        if "tls internal" in contents or "disable_http_challenge" not in lines:
            raise PreflightError(
                "public Caddyfile must use public ACME TLS-ALPN without internal certificates or HTTP-01"
            )
        if "auto_https disable_redirects" not in lines:
            raise PreflightError("public Caddyfile must not depend on the closed host HTTP port")
    elif "tls internal" not in contents:
        raise PreflightError("staging Caddyfile must use its isolated internal certificate")


def _validate_compose(
    config: dict[str, Any],
    mode: str,
    max_memory_mib: int,
    max_cpus: float,
    locked_images: set[str],
    ingress_exposure: str = "public-cutover",
) -> tuple[float, float]:
    """Enforce network exposure and aggregate cgroup limits from rendered Compose."""

    services = config.get("services")
    if not isinstance(services, dict):
        raise PreflightError("rendered Compose has no services")
    public_name = "teams-public-ingress"
    memory = 0
    cpus = 0.0
    for name, service in services.items():
        if not isinstance(service, dict):
            raise PreflightError(f"Compose service {name} is malformed")
        if (
            service.get("privileged") is True
            or service.get("network_mode") == "host"
            or service.get("pid") == "host"
        ):
            raise PreflightError(f"Compose service {name} requests host-level access")
        if service.get("image") not in locked_images:
            raise PreflightError(
                f"Compose service {name} does not use an image from the exact ARM64 lock"
            )
        ports = service.get("ports") or []
        if ports and name != public_name:
            raise PreflightError(f"only the Teams public ingress may publish host ports ({name})")
        if name == public_name:
            targets = {
                int(row.get("target", 0)): (str(row.get("published", "")), row.get("host_ip"))
                for row in ports
            }
            if set(targets) != {443} or any(row.get("protocol", "tcp") != "tcp" for row in ports):
                raise PreflightError(
                    "Teams ingress may publish only TCP 443; keep host port 80 closed"
                )
            _validate_caddyfile(service, mode, ingress_exposure=ingress_exposure)
            if (
                mode == "connector-auth-readonly"
                and ingress_exposure == "host-fronted-staging"
                and (
                    len(ports) != 1
                    or targets.get(443) != ("18443", "127.0.0.1")
                    or ports[0].get("protocol", "tcp") != "tcp"
                )
            ):
                raise PreflightError(
                    "host-fronted staging ingress must publish only 127.0.0.1:18443/tcp"
                )
            if mode in {"bootstrap", "staging", "stage1", "stage2", "stage3"}:
                if any(host_ip not in {"127.0.0.1", "::1"} for _, host_ip in targets.values()):
                    raise PreflightError("staging ingress must bind loopback only")
                if any(published == "443" for published, _ in targets.values()):
                    raise PreflightError(
                        "staging ingress must use a loopback high port rather than host 443"
                    )
            if mode in {"cutover", "connector-auth-readonly"} and not (
                mode == "connector-auth-readonly" and ingress_exposure == "host-fronted-staging"
            ):
                if {published for published, _ in targets.values()} != {"443"}:
                    raise PreflightError("cutover ingress must bind standard HTTPS port 443 only")
                if any(host_ip in {"127.0.0.1", "::1"} for _, host_ip in targets.values()):
                    raise PreflightError("cutover ingress must bind the VM public interface")
        profiles = set(service.get("profiles", []))
        if profiles & {"directory-bootstrap", "directory-operator"}:
            continue
        if name == "cloud-management-phase2":
            active_phase = (
                "runpod"
                if mode == "stage3" or (mode == "connector-auth-readonly" and "runpod" in profiles)
                else "cloud-phase2"
            )
            if (
                mode not in {"stage2", "stage3", "cutover", "connector-auth-readonly"}
                or active_phase not in profiles
            ):
                continue
        memory_value = service.get("mem_limit")
        memory_bytes = _memory_limit_bytes(memory_value)
        if memory_bytes is None:
            raise PreflightError(f"Compose service {name} lacks a finite memory limit")
        amount_mib = memory_bytes / MIB
        memory += amount_mib
        cpu_limit = _cpu_limit_decimal(service.get("cpus"))
        if cpu_limit is None:
            raise PreflightError(f"Compose service {name} lacks a finite CPU limit")
        cpus += float(cpu_limit)
    if memory > max_memory_mib:
        raise PreflightError(
            f"Compose cgroup memory limits total {memory:.0f} MiB, above {max_memory_mib} MiB"
        )
    if cpus > max_cpus:
        raise PreflightError(f"Compose CPU limits total {cpus:.2f}, above {max_cpus:.2f}")
    return memory, cpus


def _memory_headroom(
    *,
    mode: str,
    config: dict[str, Any],
    available_mib: int,
    cgroup_budget_mib: float,
    minimum_available_mib: int,
    connector_auth_phase: str | None = None,
) -> dict[str, Any]:
    """Gate new phase memory against real MemAvailable and retain a staging-only state."""

    if minimum_available_mib < 256:
        raise PreflightError("minimum available-memory reserve must be at least 256 MiB")
    additional_mib = 0
    if mode == "stage2":
        cloud = config.get("services", {}).get("cloud-management-phase2")
        limit = cloud.get("mem_limit") if isinstance(cloud, dict) else None
        if not isinstance(limit, int) or limit <= 0:
            raise PreflightError("stage 2 Cloud Management memory limit is unavailable")
        additional_mib = math.ceil(limit / MIB)
    elif mode == "connector-auth-readonly":
        if connector_auth_phase == "provider-bootstrap":
            for service_name in ("huggingface", "cloud-management-phase2"):
                service = config.get("services", {}).get(service_name)
                limit = (
                    _memory_limit_bytes(service.get("mem_limit"))
                    if isinstance(service, dict)
                    else None
                )
                if limit is None:
                    raise PreflightError(f"{service_name} memory increment is unavailable")
                additional_mib += math.ceil(limit / MIB)
        elif connector_auth_phase != "issued-connector-activation":
            raise PreflightError("connector auth phase is required for read-only capacity planning")
    required_mib = minimum_available_mib + additional_mib
    if available_mib < required_mib:
        raise PreflightError(
            f"host has {available_mib} MiB MemAvailable; this phase requires {required_mib} MiB "
            "including the configured memory reserve"
        )
    staging_measurement_required = mode != "cutover" and available_mib < (
        cgroup_budget_mib + minimum_available_mib
    )
    return {
        "status": "READY_FOR_STAGING_MEASUREMENT" if staging_measurement_required else "READY",
        "memAvailableMiB": available_mib,
        "minimumMemAvailableMiB": minimum_available_mib,
        "additionalPhaseMemoryMiB": additional_mib,
        "runtimeRssMeasurementRequired": staging_measurement_required,
    }


def _compose_config(
    docker_prefix: list[str], compose_files: list[Path], env_files: list[Path]
) -> dict[str, Any]:
    """Render Compose profiles locally for validation without starting any service."""

    argv = [*docker_prefix, "compose"]
    for env_file in env_files:
        argv.extend(["--env-file", str(env_file.resolve())])
    for compose_file in compose_files:
        argv.extend(["--file", str(compose_file.resolve())])
    # Resolve the stable owner ID for actor validation without launching the operator service.
    for profile in ACTIVE_COMPOSE_PROFILES:
        argv.extend(["--profile", profile])
    argv.extend(["config", "--format", "json"])
    output = _run(argv)
    try:
        config = json.loads(output)
    except json.JSONDecodeError as error:
        raise PreflightError(
            "Docker Compose did not return a valid rendered configuration"
        ) from error
    return config


def _validate_secrets_and_configs(
    config: dict[str, Any],
    secrets_dir: Path,
    config_dir: Path,
    mode: str,
    *,
    connector_auth_phase: str | None = None,
    notification_anchor_file: Path | None = None,
    notification_provenance_file: Path | None = None,
    candidate_runpod_key_file: Path | None = None,
) -> dict[str, str]:
    """Check every file-backed secret/config path while keeping its contents private."""

    notification_inputs: dict[str, str] = {}
    for directory, name in ((secrets_dir, "secret"), (config_dir, "configuration")):
        if not _private_artifact(directory) or directory.is_symlink() or not directory.is_dir():
            raise PreflightError(f"private {name} directory must remain under umbrella .artifacts")
        metadata = directory.stat()
        if metadata.st_uid != 10001 or metadata.st_mode & 0o077:
            raise PreflightError(
                f"private {name} directory must be owner-only and owned by UID 10001"
            )
    resolved_secrets = config.get("secrets", {})
    huggingface_enabled = _huggingface_enabled(config)
    present_huggingface_secrets = set(resolved_secrets) & HUGGINGFACE_OPTIONAL_SECRET_NAMES
    if huggingface_enabled and present_huggingface_secrets != HUGGINGFACE_OPTIONAL_SECRET_NAMES:
        raise PreflightError(
            "HF-enabled Compose must define its complete provider and OAuth secrets"
        )
    if not huggingface_enabled and present_huggingface_secrets:
        raise PreflightError("GitHub-only Compose must not define Hugging Face secrets")
    phase_secrets = {
        "cloud-management-exchange-hmac-key": {
            "stage2",
            "stage3",
            "cutover",
            "connector-auth-readonly",
        },
        "gcp-wif-credential-config": {
            "stage2",
            "stage3",
            "cutover",
            "connector-auth-readonly",
        },
        "runpod-api-key": {"stage3", "cutover", "connector-auth-readonly"},
    }
    for secret_name, secret in resolved_secrets.items():
        if secret_name in phase_secrets and mode not in phase_secrets[secret_name]:
            continue
        source = secret.get("file") if isinstance(secret, dict) else None
        if not isinstance(source, str):
            raise PreflightError(f"secret {secret_name} must resolve to one host file")
        path = Path(source)
        if secret_name == "account-center-huggingface-oauth-source":
            if mode == "connector-auth-readonly":
                candidate_directory = path.parent
                candidates_directory = candidate_directory.parent
                private_directory = candidates_directory.parent
                onboarding_directory = private_directory.parent
                if (
                    path.name != "huggingface-oauth.json"
                    or not re.fullmatch(r"[0-9a-f]{32}", candidate_directory.name)
                    or candidates_directory.name != "provider-candidates"
                    or private_directory.name != "private"
                    or onboarding_directory.name != "onboarding"
                    or not _private_artifact(onboarding_directory)
                    or onboarding_directory.is_symlink()
                    or not onboarding_directory.is_dir()
                    or onboarding_directory.stat().st_uid != 10001
                    or onboarding_directory.stat().st_mode & 0o077
                    or not _private_artifact(private_directory)
                    or private_directory.is_symlink()
                    or not private_directory.is_dir()
                    or private_directory.stat().st_uid != 10001
                    or stat.S_IMODE(private_directory.stat().st_mode) != 0o700
                    or not _private_artifact(candidates_directory)
                    or candidates_directory.is_symlink()
                    or not candidates_directory.is_dir()
                    or candidates_directory.stat().st_uid != 10001
                    or stat.S_IMODE(candidates_directory.stat().st_mode) != 0o700
                    or not _private_artifact(candidate_directory)
                    or candidate_directory.is_symlink()
                    or not candidate_directory.is_dir()
                    or candidate_directory.stat().st_uid != 10001
                    or stat.S_IMODE(candidate_directory.stat().st_mode) != 0o700
                ):
                    raise PreflightError(
                        "candidate HF OAuth seed must stay in its versioned owner-only onboarding bundle"
                    )
                if stat.S_IMODE(path.stat().st_mode) != 0o400:
                    raise PreflightError(
                        "candidate HF OAuth seed must use owner-read-only mode 0400"
                    )
            else:
                onboarding_directory = path.parent.parent
                if (
                    path.name != "huggingface-oauth.json"
                    or path.parent.name != "private"
                    or onboarding_directory.name != "onboarding"
                    or not _private_artifact(onboarding_directory)
                    or onboarding_directory.is_symlink()
                    or not onboarding_directory.is_dir()
                    or onboarding_directory.stat().st_uid != 10001
                    or onboarding_directory.stat().st_mode & 0o077
                    or not _private_artifact(path.parent)
                    or path.parent.is_symlink()
                    or not path.parent.is_dir()
                    or path.parent.stat().st_uid != 10001
                    or path.parent.stat().st_mode & 0o077
                ):
                    raise PreflightError(
                        "Hugging Face OAuth bootstrap must stay in its owner-only private onboarding directory"
                    )
            _secret_file(path, secret_name)
            _validate_huggingface_oauth_source(path)
            continue
        if secret_name == "runpod-api-key" and mode == "connector-auth-readonly":
            if candidate_runpod_key_file is None:
                raise PreflightError("an explicit candidate RunPod key file is required")
            try:
                expected_key_path = candidate_runpod_key_file.resolve(strict=True)
                rendered_key_path = path.resolve(strict=True)
                metadata = expected_key_path.lstat()
            except OSError as error:
                raise PreflightError("candidate RunPod key file is unavailable") from error
            if (
                expected_key_path != rendered_key_path
                or not _private_artifact(expected_key_path)
                or expected_key_path.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 10001
                or metadata.st_mode & 0o077
                or stat.S_IMODE(metadata.st_mode) != 0o400
                or metadata.st_size < 16
            ):
                raise PreflightError(
                    "RunPod Compose secret must be the exact owner-only candidate API key file"
                )
            _secret_file(expected_key_path, secret_name)
            continue
        if path.parent.resolve() != secrets_dir.resolve():
            raise PreflightError(
                f"secret {secret_name} is outside the configured private secret directory"
            )
        _secret_file(path, path.name)
    _validate_directory_database_urls(secrets_dir)
    _validate_internal_service_certificates(secrets_dir)
    _validate_computer_proxy_certificate(config, secrets_dir)
    _validate_exchange_authority_identity(config, secrets_dir)
    resolved_configs = config.get("configs", {})
    for config_name, row in resolved_configs.items():
        source = row.get("file") if isinstance(row, dict) else None
        if not isinstance(source, str):
            raise PreflightError(f"config {config_name} must resolve to one private config file")
        path = Path(source)
        if path.parent.resolve() != config_dir.resolve() or path.is_symlink() or not path.is_file():
            raise PreflightError(
                f"config {config_name} is not a regular file in the configured private config directory"
            )
        _validate_runtime_config_file(path, config_name)
        if path.name == "authority-signing-keyring.json":
            authority_env = (
                config.get("services", {}).get("workspace-authority", {}).get("environment", {})
            )
            _validate_keyring(path, authority_env.get("CYRENE_AUTHORITY_SIGNING_KEY_ID"))
        elif path.name == "connector-bindings.json":
            _validate_connector_bindings(
                path,
                _expected_connector_providers(config, mode, connector_auth_phase),
                allow_empty=mode == "bootstrap",
            )
        elif path.name == "connector-tool-catalog.json":
            _validate_connector_tool_catalog(
                path,
                config_dir / "connector-bindings.json",
                _expected_connector_providers(config, mode, connector_auth_phase),
                allow_empty=mode == "bootstrap",
            )
        elif path.name == "navigator-principal-config.json":
            _validate_principal_configuration(
                path,
                config.get("services", {}),
                connector_auth_phase=connector_auth_phase,
            )
        elif path.name == "responsibility-targets.v1.json":
            _validate_responsibility_targets(
                path,
                config_dir / "connector-bindings.json",
                allow_pending=mode == "bootstrap",
            )
        elif path.name == "connector-bootstrap.json":
            _validate_connector_bootstrap_config(
                path,
                huggingface_enabled=huggingface_enabled,
                expected_actor=_connector_bootstrap_actor(config),
                expected_cloud_profiles=_connector_bootstrap_cloud_profiles(config, mode),
            )
        elif path.suffix == ".json":
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise PreflightError(f"config {config_name} is invalid JSON") from error
            if PLACEHOLDER_RE.search(json.dumps(document, sort_keys=True)):
                raise PreflightError(f"config {config_name} still contains a placeholder")
    _validate_actor_projection(config_dir / "teams-actor-subjects.json", config.get("services", {}))
    account_center = config.get("services", {}).get("account-center")
    if isinstance(account_center, dict):
        _validate_account_center_config(
            account_center,
            huggingface_enabled=huggingface_enabled,
        )
    cloud_service = config.get("services", {}).get("cloud-management-phase2")
    if isinstance(cloud_service, dict) and mode in {
        "stage2",
        "stage3",
        "cutover",
        "connector-auth-readonly",
    }:
        _validate_cloud_acceptance_mount(cloud_service)
    oauth_mounts = [
        service_name
        for service_name, service in config.get("services", {}).items()
        for row in service.get("secrets", [])
        if isinstance(row, dict) and row.get("source") == "account-center-huggingface-oauth-source"
    ]
    expected_oauth_mounts = ["account-center"] if huggingface_enabled else []
    if oauth_mounts != expected_oauth_mounts:
        raise PreflightError(
            "Hugging Face OAuth bootstrap secret mounts must match the selected provider profile"
        )
    teams_provider = config.get("services", {}).get("teams-provider")
    navigator = config.get("services", {}).get("navigator")
    if isinstance(teams_provider, dict) and isinstance(navigator, dict):
        _validate_teams_connector_config(teams_provider, navigator)
    if mode == "connector-auth-readonly":
        bindings_path = config_dir / "connector-bindings.json"
        try:
            bindings_document = json.loads(
                bindings_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise PreflightError("connector read-only binding file is unavailable") from error
        if connector_auth_phase == "provider-bootstrap":
            _validate_exchange_authorization_cloud_runner_phase(config, connector_auth_phase)
            if "teams-credential-notifications" in resolved_configs or any(
                row.get("source") == "teams-credential-notifications"
                for row in config.get("services", {}).get("navigator", {}).get("configs", [])
                if isinstance(row, dict)
            ):
                raise PreflightError(
                    "notification config must remain disabled during provider bootstrap"
                )
            notification_environment = (
                config.get("services", {}).get("navigator", {}).get("environment", {})
            )
            if "TEAMS_CREDENTIAL_NOTIFICATIONS_CONFIG_FILE" in notification_environment:
                raise PreflightError(
                    "notification config path must remain absent during provider bootstrap"
                )
        elif connector_auth_phase == "issued-connector-activation":
            _validate_exchange_authorization_cloud_runner_phase(config, connector_auth_phase)
            expected_provider_ids = _expected_connector_providers(
                config, mode, connector_auth_phase
            ) - {"github", "computer"}
            _validate_new_provider_bindings_readonly(bindings_document, expected_provider_ids)
            notification_inputs = _validate_credential_notification_activation(
                config,
                config_dir=config_dir,
                anchor_file=notification_anchor_file,
                provenance_file=notification_provenance_file,
                expected_provider_ids=expected_provider_ids,
            )
        else:
            raise PreflightError("connector auth phase is required for read-only configuration")
    return notification_inputs


def _validate_runtime_config_file(path: Path, name: str) -> None:
    """Require Compose-mounted config to be readable by its fixed runtime UID."""

    metadata = path.stat()
    _validate_runtime_config_metadata(metadata.st_uid, metadata.st_mode, name)


def _validate_runtime_config_metadata(owner_uid: int, mode: int, name: str) -> None:
    """Validate config owner/read bits without relying on the current host UID."""

    if owner_uid != 10001:
        raise PreflightError(f"Compose config {name} must be owned by runtime UID 10001")
    if mode & 0o077 or not mode & 0o400:
        raise PreflightError(
            f"Compose config {name} must be owner-readable and have no group/world access"
        )


def _validate_identity_scope(config: dict[str, Any], actor_projection_path: Path) -> None:
    """Require one tenant, one team, one owner, and an approved model profile."""

    services = config.get("services", {})
    navigator = services.get("navigator", {}).get("environment", {})
    ingress = services.get("teams-ingress", {}).get("environment", {})
    authority = services.get("workspace-authority", {}).get("environment", {})
    operator = services.get("directory-operator", {}).get("environment", {})
    tenant = navigator.get("TEAMS_TENANT_ID")
    authority_tenant = authority.get("CYRENE_AUTHORITY_AAD_TENANT_ID")
    team_ids = navigator.get("TEAMS_ALLOWED_TEAM_IDS", "").split(",")
    try:
        actor_subjects = json.loads(actor_projection_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreflightError("dynamic Teams actor-subject projection is unavailable") from error
    owner_id = operator.get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID")
    if not isinstance(tenant, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", tenant
    ):
        raise PreflightError("Teams tenant ID must be a verified Entra tenant UUID")
    if authority_tenant != tenant or ingress.get("TENANT_ID") != tenant:
        raise PreflightError("Teams, ingress, and Workspace Authority must use the same tenant")
    if ingress.get("CLIENT_ID") != navigator.get("TEAMS_BOT_APP_ID"):
        raise PreflightError("Teams ingress and Navigator must use the same bot application ID")
    bot_app_id = navigator.get("TEAMS_BOT_APP_ID")
    if (
        not isinstance(bot_app_id, str)
        or not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            bot_app_id,
        )
        or ingress.get("TEAMS_BOT_APP_ID") != bot_app_id
    ):
        raise PreflightError("Teams ingress must receive the dedicated bot application ID")
    if ingress.get("TEAMS_TENANT_ID") != tenant:
        raise PreflightError("Teams ingress settings must match the Navigator tenant")
    if (
        navigator.get("TEAMS_TASK_ACTOR_SUBJECTS_FILE")
        != "/run/teams-authorization/teams-actor-subjects.json"
    ):
        raise PreflightError("Navigator must use the request-refreshed owner subject projection")
    if len(team_ids) != 1 or not team_ids[0] or team_ids[0].strip() != team_ids[0]:
        raise PreflightError("production admission must allow exactly one Teams team ID")
    if not isinstance(owner_id, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", owner_id
    ):
        raise PreflightError(
            "Directory operator identity must be one verified owner AAD object UUID"
        )
    if owner_id not in actor_subjects.get("subjects", []):
        raise PreflightError(
            "Directory operator owner must be present in the dynamic Teams admission projection"
        )
    _validate_model_runtime_config(navigator)


def _validate_model_runtime_config(navigator: dict[str, Any]) -> None:
    """Validate the selected DSH or native CodeBuddy ACP model path."""

    backend = navigator.get("CYRENE_HARNESS_EXECUTION_BACKEND", "dsh")
    source = navigator.get("CYRENE_HARNESS_MODEL_SOURCE")
    model = navigator.get("CYRENE_HARNESS_MODEL")
    concurrency = navigator.get("CYRENE_EXECUTOR_MAX_CONCURRENT_TASKS")
    if backend == "codebuddy-acp":
        if source not in {None, ""} or model not in {None, ""}:
            raise PreflightError(
                "CodeBuddy ACP must not select the legacy Harness model source or model"
            )
        if navigator.get("CYRENE_HARNESS_CODEBUDDY_ACP_COMMAND") != "codebuddy":
            raise PreflightError("CodeBuddy ACP command must be exactly the native codebuddy CLI")
        if navigator.get("CYRENE_HARNESS_CODEBUDDY_ACP_MODEL") not in {
            "glm-5.3-flash",
            "deepseek-v4.1-flash",
        }:
            raise PreflightError("CodeBuddy ACP model must be an approved GLM or DeepSeek profile")
        if navigator.get("CYRENE_HARNESS_CODEBUDDY_FALLBACK_ENABLED") != "false":
            raise PreflightError("CodeBuddy ACP must disable model fallback")
        if navigator.get("CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP") != "true":
            raise PreflightError("CodeBuddy ACP requires the owner no-paid-topup declaration")
        if navigator.get("CYRENE_HARNESS_CODEBUDDY_FALLBACK_MODEL") not in {None, ""}:
            raise PreflightError("CodeBuddy ACP must not configure a legacy fallback model")
        if concurrency != "2":
            raise PreflightError(
                "CodeBuddy ACP requires two bounded foreground/background task slots"
            )
        return
    if backend != "dsh":
        raise PreflightError("Navigator execution backend must be dsh or codebuddy-acp")
    if source == "antigravity":
        if model != "gemini-3.8-flash-medium":
            raise PreflightError("proactive Navigator must use Antigravity gemini-3.8-flash-medium")
        if (
            navigator.get("CYRENE_HARNESS_CODEBUDDY_FALLBACK_ENABLED") != "true"
            or navigator.get("CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP") != "true"
            or navigator.get("CYRENE_HARNESS_CODEBUDDY_FALLBACK_MODEL")
            not in {"glm-5.3-flash", "deepseek-v4.1-flash"}
        ):
            raise PreflightError(
                "proactive Navigator fallback must be an approved no-additional-spend CodeBuddy model"
            )
        if concurrency != "2":
            raise PreflightError(
                "proactive Navigator requires two bounded slots for foreground chat and background work"
            )
        return
    if source != "codebuddy" or model not in {"glm-5.3-flash", "deepseek-v4.1-flash"}:
        raise PreflightError(
            "Navigator must use an explicitly approved real model source and model"
        )
    if concurrency not in {"1", "2"}:
        raise PreflightError("Navigator task concurrency must fit the two-CPU deployment profile")


def _validate_codebuddy_acp_boundary(
    config: dict[str, Any],
    *,
    secrets_dir: Path,
    config_dir: Path,
    connector_auth_phase: str | None = None,
) -> None:
    """Keep the ACP bridge private to Navigator and out of Compose credentials.

    中文：MCP bridge 配置由 Navigator 在容器内逐次生成；禁止通过 Compose 注入 token 或文件挂载。
    """

    navigator = config.get("services", {}).get("navigator", {})
    environment = navigator.get("environment", {})
    volumes = navigator.get("volumes", [])
    secrets = navigator.get("secrets", [])
    configs = navigator.get("configs", [])
    if environment.get("CYRENE_WORKFLOWS_ENABLED") not in {"false", "true"}:
        raise PreflightError("CodeBuddy ACP Workflows engine must be explicitly false or true")
    if (
        navigator.get("read_only") is not True
        or "ALL" not in navigator.get("cap_drop", [])
        or "no-new-privileges:true" not in navigator.get("security_opt", [])
        or "/tmp:rw,nosuid,nodev,size=128m" not in navigator.get("tmpfs", [])
    ):
        raise PreflightError(
            "CodeBuddy ACP requires a read-only root, dropped capabilities, and bounded 128 MiB temporary storage"
        )
    if navigator.get("command") or navigator.get("entrypoint"):
        raise PreflightError("CodeBuddy ACP must retain the Navigator-owned fixed CLI invocation")
    if (
        navigator.get("privileged") is True
        or navigator.get("cap_add")
        or navigator.get("ports")
        or navigator.get("network_mode") == "host"
    ):
        raise PreflightError(
            "CodeBuddy ACP must keep the existing low-privilege private service boundary"
        )
    if any("MCP" in str(name).upper() for name in environment):
        raise PreflightError(
            "CodeBuddy ACP MCP configuration must not be injected through Compose env"
        )
    if any(
        "MCP" in str(value).upper()
        for row in [*volumes, *secrets, *configs]
        if isinstance(row, dict)
        for value in row.values()
    ):
        raise PreflightError(
            "CodeBuddy ACP MCP configuration must stay out of Compose mounts and secrets"
        )
    if any(isinstance(row, dict) and row.get("target") == "/home/navigator" for row in volumes):
        raise PreflightError("CodeBuddy ACP must not mount the legacy Antigravity model home")
    _validate_codebuddy_acp_mount_boundary(
        config,
        navigator,
        secrets_dir=secrets_dir,
        config_dir=config_dir,
        connector_auth_phase=connector_auth_phase,
    )


def _validate_codebuddy_acp_mount_boundary(
    config: dict[str, Any],
    navigator: dict[str, Any],
    *,
    secrets_dir: Path,
    config_dir: Path,
    connector_auth_phase: str | None = None,
) -> None:
    """Keep ACP mounts to the base set plus the issued-activation notification config."""

    volumes = navigator.get("volumes", [])
    by_target = {
        row.get("target"): row for row in volumes if isinstance(row, dict) and row.get("target")
    }
    allowed_targets = {
        "/home/navigator/.codebuddy",
        "/var/lib/cyrene-navigator",
        "/run/teams-authorization",
    }
    automation_overlay = _automation_workflow_markers_present(
        config.get("services", {}), config.get("secrets")
    )
    if automation_overlay:
        allowed_targets.add(AUTOMATION_HEALTH_TARGET)
    session_volume = by_target.get("/var/lib/cyrene-navigator", {})
    codebuddy_home = by_target.get("/home/navigator/.codebuddy", {})
    actor_projection = by_target.get("/run/teams-authorization", {})
    project_name = config.get("name")
    volume_definitions = config.get("volumes", {})
    volume_definition = (
        volume_definitions.get("navigator-sessions", {})
        if isinstance(volume_definitions, dict)
        else {}
    )
    expected_volume_name = (
        f"{project_name}_navigator-sessions" if isinstance(project_name, str) else None
    )
    secrets = navigator.get("secrets", [])
    configs = navigator.get("configs", [])
    secret_pairs = {
        (row.get("source"), row.get("target")) for row in secrets if isinstance(row, dict)
    }
    config_pairs = {
        (row.get("source"), row.get("target")) for row in configs if isinstance(row, dict)
    }
    expected_config_pairs = {
        ("connector-bindings", "/run/config/connector-bindings.json"),
        ("connector-tool-catalog", "/run/config/connector-tool-catalog.json"),
        ("responsibility-targets", "/run/config/responsibility-targets.v1.json"),
    }
    expected_notification_consumers = []
    if connector_auth_phase == "issued-connector-activation":
        notification_target = "/run/secrets/teams-credential-notifications.json"
        expected_config_pairs.add(
            (
                "teams-credential-notifications",
                notification_target,
            )
        )
        expected_notification_consumers = [("navigator", notification_target)]
    expected_secret_pairs = {
        ("navigator-exchange-hmac-key", "navigator-exchange-hmac-key"),
        ("navigator-session-token", "navigator-session-token"),
        ("teams-navigator-hmac-key", "teams-navigator-hmac-key"),
        ("internal-service-root-ca", "internal-service-root-ca"),
    }
    if automation_overlay:
        expected_secret_pairs |= {(name, name) for name in AUTOMATION_WORKFLOW_SECRET_NAMES}
    notification_consumers = [
        (service_name, row.get("target"))
        for service_name, service in config.get("services", {}).items()
        if isinstance(service, dict)
        for row in service.get("configs", [])
        if isinstance(row, dict) and row.get("source") == "teams-credential-notifications"
    ]
    notification_consumer_is_exact = (
        len(notification_consumers) == len(expected_notification_consumers)
        and notification_consumers == expected_notification_consumers
    )
    if (
        len(volumes) != (4 if automation_overlay else 3)
        or set(by_target) != allowed_targets
        or codebuddy_home.get("type") != "bind"
        or codebuddy_home.get("read_only", False) is not False
        or not isinstance(codebuddy_home.get("source"), str)
        or not codebuddy_home.get("source")
        or Path(str(codebuddy_home.get("source", ""))).resolve()
        != (secrets_dir / "codebuddy").resolve()
        or session_volume.get("type") != "volume"
        or session_volume.get("source") != "navigator-sessions"
        or not isinstance(project_name, str)
        or not isinstance(volume_definition, dict)
        or volume_definition.get("name") != expected_volume_name
        or actor_projection.get("type") != "bind"
        or actor_projection.get("read_only") is not True
        or not isinstance(actor_projection.get("source"), str)
        or not actor_projection.get("source")
        or Path(str(actor_projection.get("source", ""))).resolve() != config_dir.resolve()
        or secret_pairs != expected_secret_pairs
        or len(secrets) != len(expected_secret_pairs)
        or config_pairs != expected_config_pairs
        or len(configs) != len(expected_config_pairs)
        or not notification_consumer_is_exact
    ):
        raise PreflightError(
            "CodeBuddy ACP Navigator must keep only its approved login, session, and Teams authorization mounts"
        )


def _validate_codebuddy_mount(config: dict[str, Any], secrets_dir: Path) -> None:
    """Require a separate service-owned CodeBuddy login directory, never a personal profile."""

    navigator = config.get("services", {}).get("navigator", {})
    matches = [
        row
        for row in navigator.get("volumes", [])
        if isinstance(row, dict) and row.get("target") == "/home/navigator/.codebuddy"
    ]
    if len(matches) != 1:
        raise PreflightError(
            "Navigator must mount exactly one service-owned CodeBuddy configuration directory"
        )
    volume = matches[0]
    path = Path(volume.get("source", ""))
    if (
        volume.get("type") != "bind"
        or volume.get("read_only") is True
        or path != secrets_dir / "codebuddy"
    ):
        raise PreflightError(
            "CodeBuddy state must be a separate writable private artifact directory"
        )
    if (
        path.is_symlink()
        or not path.is_dir()
        or path.stat().st_uid != 10001
        or path.stat().st_mode & 0o077
    ):
        raise PreflightError(
            "service-owned CodeBuddy directory must be owner-only and owned by UID 10001"
        )


def _validate_model_home_mount(config: dict[str, Any]) -> None:
    """Keep Antigravity credentials and CLI caches in a private persistent home volume."""

    navigator = config.get("services", {}).get("navigator", {})
    matches = [
        row
        for row in navigator.get("volumes", [])
        if isinstance(row, dict) and row.get("target") == "/home/navigator"
    ]
    if len(matches) != 1:
        raise PreflightError("Antigravity requires one separate persistent Navigator model home")
    volume = matches[0]
    path = Path(volume.get("source", ""))
    if (
        volume.get("type") != "bind"
        or volume.get("read_only") is True
        or not _private_artifact(path)
        or path.is_symlink()
        or not path.is_dir()
    ):
        raise PreflightError("Navigator model home must be a writable private artifact directory")
    metadata = path.stat()
    if metadata.st_uid != 10001 or metadata.st_mode & 0o077:
        raise PreflightError("Navigator model home must be owner-only and owned by UID 10001")


def _validate_proactive_navigator_container(config: dict[str, Any]) -> None:
    """Restrict Navigator writes to explicit state/auth mounts and drop Linux capabilities."""

    navigator = config.get("services", {}).get("navigator", {})
    caps = navigator.get("cap_drop", [])
    security = navigator.get("security_opt", [])
    tmpfs = navigator.get("tmpfs", [])
    if (
        navigator.get("read_only") is not True
        or "ALL" not in caps
        or "no-new-privileges:true" not in security
        or not any(str(row).split(":", 1)[0] == "/tmp" for row in tmpfs)
    ):
        raise PreflightError(
            "proactive Navigator requires a read-only root, dropped capabilities, and bounded temporary storage"
        )
    target_path = navigator.get("environment", {}).get("CYRENE_RESPONSIBILITY_TARGETS_FILE")
    config_mounts = navigator.get("configs", [])
    target_mounts = [
        row
        for row in config_mounts
        if isinstance(row, dict)
        and row.get("source") == "responsibility-targets"
        and row.get("target") == "/run/config/responsibility-targets.v1.json"
    ]
    if target_path != "/run/config/responsibility-targets.v1.json" or len(target_mounts) != 1:
        raise PreflightError(
            "proactive Navigator must read host-owned responsibility targets from its read-only config mount"
        )


def _validate_responsibility_targets(
    path: Path, bindings_path: Path, *, allow_pending: bool = False
) -> None:
    """Allow current GitHub bindings, or explicit unresolved IDs during bootstrap only."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        bindings = json.loads(bindings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreflightError(
            "responsibility targets or connector bindings are invalid JSON"
        ) from error
    if (
        not isinstance(document, dict)
        or set(document) != {"schemaVersion", "targets"}
        or document.get("schemaVersion") != 1
        or isinstance(document.get("schemaVersion"), bool)
        or not isinstance(document.get("targets"), list)
        or not isinstance(bindings, dict)
        or not isinstance(bindings.get("bindings"), list)
    ):
        raise PreflightError("responsibility targets have an unsupported schema")
    github_bindings = {
        row.get("bindingId")
        for row in bindings["bindings"]
        if isinstance(row, dict) and row.get("providerId") == "github"
    }
    if not document["targets"]:
        raise PreflightError("responsibility targets must include the approved repository set")
    seen_ids: set[str] = set()
    seen_labels: set[str] = set()
    expected_grants = {
        "draft_pull_request": {
            "id": "draft_pull_request",
            "action": "draft_pull_request",
            "baseBranches": ["develop"],
            "branchPrefixes": ["cyrene/assistant/"],
            "pathPrefixes": ["**"],
            "issueNumbers": [],
            "sourceWorkflowIds": [],
            "permittedOperations": [
                "github.branch.create",
                "github.file.commit",
                "github.pull_request.draft.create",
            ],
        },
        "rerun_source_ci": {
            "id": "rerun_source_ci",
            "action": "rerun_source_ci",
            "baseBranches": [],
            "branchPrefixes": ["cyrene/assistant/"],
            "pathPrefixes": [],
            "issueNumbers": [],
            "sourceWorkflowIds": None,
            "permittedOperations": ["github.actions.rerun_failed"],
        },
    }
    for target in document["targets"]:
        if not isinstance(target, dict) or set(target) != {
            "id",
            "label",
            "bindingId",
            "uri",
            "actionGrants",
        }:
            raise PreflightError("responsibility target does not match its closed schema")
        repository_id = target.get("id")
        label = target.get("label")
        binding_id = target.get("bindingId")
        if (
            not isinstance(repository_id, str)
            or not re.fullmatch(r"[1-9][0-9]*", repository_id)
            or repository_id in seen_ids
            or not isinstance(label, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", label)
            or label in seen_labels
            or target.get("uri") != f"https://github.com/{label}"
            or not isinstance(binding_id, str)
            or (
                binding_id not in github_bindings
                and not (allow_pending and binding_id == "UNBOUND_PENDING_EXCHANGE_GITHUB_REBIND")
            )
        ):
            raise PreflightError(
                "responsibility targets must use unique canonical repos and a current Exchange GitHub binding"
            )
        seen_ids.add(repository_id)
        seen_labels.add(label)
        grants = target.get("actionGrants")
        if not isinstance(grants, list) or not grants or len(grants) > len(expected_grants):
            raise PreflightError(
                "responsibility grants must include draft work and contain only the approved actions"
            )
        grant_map = {
            row.get("id"): row for row in grants if isinstance(row, dict) and row.get("id")
        }
        if len(grant_map) != len(grants) or not set(grant_map) <= set(expected_grants):
            raise PreflightError(
                "responsibility grants must include draft work and contain only the approved actions"
            )
        if "draft_pull_request" not in grant_map:
            raise PreflightError("every responsibility target must grant the approved draft action")
        for action_id, grant in grant_map.items():
            expected = expected_grants[action_id]
            if action_id == "rerun_source_ci":
                workflow_ids = grant.get("sourceWorkflowIds")
                pending_workflow_ids = allow_pending and workflow_ids == [
                    "REPLACE_WITH_APPROVED_SOURCE_WORKFLOW_ID"
                ]
                if (
                    not isinstance(workflow_ids, list)
                    or not workflow_ids
                    or (
                        not pending_workflow_ids
                        and any(
                            not isinstance(workflow_id, str)
                            or not re.fullmatch(r"[1-9][0-9]{0,19}", workflow_id)
                            for workflow_id in workflow_ids
                        )
                    )
                    or len(set(workflow_ids)) != len(workflow_ids)
                    or {**expected, "sourceWorkflowIds": workflow_ids} != grant
                ):
                    raise PreflightError(
                        "CI rerun grants require explicit source workflow IDs and the approved assistant branch prefix"
                    )
            elif grant != expected:
                raise PreflightError(
                    "responsibility grants must contain only the approved assistant draft and CI actions"
                )


def _validate_github_workflow_allowlist(compose: dict[str, Any], config_dir: Path) -> None:
    """Require one verified source-CI inventory to match provider and Navigator rerun scopes."""

    artifact_root = config_dir.resolve().parent
    inventory_path = artifact_root / "github-source-workflows.json"
    provider_fragment_path = config_dir / "github-issues-source-workflow-ids.v1.json"
    targets_path = config_dir / "responsibility-targets.v1.json"
    workflow_path = artifact_root / "proactive-assistant-workflow.v2.json"

    def read_private_json(path: Path, label: str) -> dict[str, Any]:
        if (
            path.is_symlink()
            or not path.is_file()
            or not _private_artifact(path)
            or path.stat().st_mode & 0o077
        ):
            raise PreflightError(f"{label} must be an owner-only file under private .artifacts")
        try:
            document = json.loads(
                path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise PreflightError(f"{label} is invalid JSON") from error
        if not isinstance(document, dict):
            raise PreflightError(f"{label} must be a JSON object")
        return document

    inventory = read_private_json(inventory_path, "GitHub source-workflow inventory")
    provider_fragment = read_private_json(
        provider_fragment_path, "GitHub provider workflow fragment"
    )
    targets_document = read_private_json(targets_path, "Navigator responsibility targets")
    workflow = read_private_json(workflow_path, "Navigator responsibility WorkFlow")
    _validate_workflow_definition_disabled(workflow)
    _validate_workflow_model_policy(compose, workflow)
    if (
        set(inventory) != {"schemaVersion", "observedAt", "source", "repositories", "excluded"}
        or inventory.get("schemaVersion") != 1
        or isinstance(inventory.get("schemaVersion"), bool)
        or not isinstance(inventory.get("observedAt"), str)
        or inventory.get("source")
        != "GitHub REST GET /repos/{owner}/{repo}/actions/workflows?per_page=100"
    ):
        raise PreflightError("GitHub source-workflow inventory has an unsupported schema or source")
    source_repositories = inventory.get("repositories")
    excluded_repositories = inventory.get("excluded")
    if (
        not isinstance(source_repositories, dict)
        or len(source_repositories) != 9
        or not isinstance(excluded_repositories, dict)
        or set(excluded_repositories) != {"DoHorizon-AI/Cyrene-Workspace"}
        or not isinstance(excluded_repositories["DoHorizon-AI/Cyrene-Workspace"], dict)
        or not isinstance(excluded_repositories["DoHorizon-AI/Cyrene-Workspace"].get("reason"), str)
        or not excluded_repositories["DoHorizon-AI/Cyrene-Workspace"]["reason"]
    ):
        raise PreflightError(
            "source-workflow inventory must cover nine source repos and exclude Workspace"
        )

    target_rows = targets_document.get("targets")
    if (
        targets_document.get("schemaVersion") != 1
        or isinstance(targets_document.get("schemaVersion"), bool)
        or not isinstance(target_rows, list)
        or len(target_rows) != 10
    ):
        raise PreflightError(
            "source-workflow inventory requires the exact ten Navigator repository targets"
        )
    targets_by_label: dict[str, dict[str, Any]] = {}
    targets_by_id: dict[str, dict[str, Any]] = {}
    for target in target_rows:
        if not isinstance(target, dict):
            raise PreflightError("Navigator repository target is malformed")
        label = target.get("label")
        repository_id = target.get("id")
        if (
            not isinstance(label, str)
            or label in targets_by_label
            or not isinstance(repository_id, str)
            or repository_id in targets_by_id
        ):
            raise PreflightError("Navigator repository target labels and IDs must be unique")
        targets_by_label[label] = target
        targets_by_id[repository_id] = target
    expected_labels = set(source_repositories) | {"DoHorizon-AI/Cyrene-Workspace"}
    if set(targets_by_label) != expected_labels:
        raise PreflightError(
            "verified source-workflow repos do not match the exact ten Navigator targets"
        )

    expected_ids_by_repo: dict[str, list[str]] = {}
    all_workflow_ids: set[str] = set()
    for label, row in source_repositories.items():
        if (
            not isinstance(label, str)
            or not isinstance(row, dict)
            or set(row) != {"id", "name", "path"}
            or not isinstance(row.get("id"), str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", row["id"])
            or row.get("path")
            not in {
                ".github/workflows/ci.yml",
                ".github/workflows/public-ci.yml",
            }
            or not isinstance(row.get("name"), str)
            or not row["name"]
            or row["id"] in all_workflow_ids
        ):
            raise PreflightError(
                "source-workflow inventory contains a duplicate or non-source workflow ID"
            )
        all_workflow_ids.add(row["id"])
        repository_id = targets_by_label[label]["id"]
        if not isinstance(repository_id, str) or not re.fullmatch(r"[1-9][0-9]*", repository_id):
            raise PreflightError("source-workflow repository target has an invalid numeric ID")
        expected_ids_by_repo[repository_id] = [row["id"]]

    workspace_target = targets_by_label["DoHorizon-AI/Cyrene-Workspace"]
    workspace_grants = workspace_target.get("actionGrants", [])
    if any(
        isinstance(grant, dict) and grant.get("id") == "rerun_source_ci"
        for grant in workspace_grants
    ):
        raise PreflightError("Workspace must remain excluded from source-CI rerun grants")
    for label in source_repositories:
        target = targets_by_label[label]
        grants = target.get("actionGrants", [])
        matches = [
            grant
            for grant in grants
            if isinstance(grant, dict) and grant.get("id") == "rerun_source_ci"
        ]
        if (
            len(matches) != 1
            or matches[0].get("sourceWorkflowIds") != expected_ids_by_repo[target["id"]]
        ):
            raise PreflightError(
                "Navigator per-repository rerun grant differs from the verified inventory"
            )
        if matches[0].get("branchPrefixes") != ["cyrene/assistant/"]:
            raise PreflightError(
                "Navigator CI reruns must stay on the assistant task-branch namespace"
            )

    expected_provider_map = expected_ids_by_repo
    if (
        set(provider_fragment) != {"source_workflow_ids"}
        or provider_fragment.get("source_workflow_ids") != expected_provider_map
    ):
        raise PreflightError(
            "private GitHub provider workflow fragment differs from the verified inventory"
        )
    github_service = compose.get("services", {}).get("github-issues", {})
    provider_environment = github_service.get("environment", {})
    try:
        provider_configuration = json.loads(
            provider_environment.get("CYRENE_CAPABILITY_CONFIGURATION_JSON", ""),
            object_pairs_hook=_unique_json_object,
        )
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError("GitHub provider activation configuration is invalid JSON") from error
    if not isinstance(provider_configuration, dict):
        raise PreflightError("GitHub provider activation configuration must be an object")
    provider_rows = provider_configuration.get("repositories")
    if not isinstance(provider_rows, list) or len(provider_rows) != 10:
        raise PreflightError(
            "GitHub provider profile must be narrowed to the exact ten Cyrene repos"
        )
    provider_repository_ids: set[str] = set()
    for row in provider_rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("repository_id"), str)
            or row["repository_id"] not in targets_by_id
            or row["repository_id"] in provider_repository_ids
        ):
            raise PreflightError(
                "GitHub provider profile contains an extra or duplicate repository"
            )
        target = targets_by_id[row["repository_id"]]
        if (
            row.get("owner") != target["label"].split("/", 1)[0]
            or row.get("name") != target["label"].split("/", 1)[1]
        ):
            raise PreflightError(
                "GitHub provider repository identity differs from Navigator targets"
            )
        provider_repository_ids.add(row["repository_id"])
    if provider_repository_ids != set(targets_by_id):
        raise PreflightError(
            "GitHub provider profile does not cover exactly the ten Navigator targets"
        )
    if provider_configuration.get("source_workflow_ids") != expected_provider_map:
        raise PreflightError("GitHub provider activation workflow IDs differ from Navigator grants")

    workflow_targets = workflow.get("targets")
    workflow_policy = workflow.get("actionPolicy")
    allowed_actions = (
        workflow_policy.get("allowedActions") if isinstance(workflow_policy, dict) else None
    )
    if (
        not isinstance(workflow_targets, list)
        or len(workflow_targets) != 10
        or not isinstance(allowed_actions, list)
    ):
        raise PreflightError("Navigator WorkFlow targets and action grants are incomplete")
    workflow_target_ids: set[str] = set()
    for target in workflow_targets:
        if not isinstance(target, dict):
            raise PreflightError("Navigator WorkFlow target is malformed")
        repository_id = target.get("id")
        if (
            target.get("kind") != "github_repository"
            or not isinstance(repository_id, str)
            or repository_id not in targets_by_id
            or repository_id in workflow_target_ids
            or target.get("label") != targets_by_id[repository_id]["label"]
            or target.get("uri") != targets_by_id[repository_id]["uri"]
            or target.get("bindingId") != targets_by_id[repository_id]["bindingId"]
        ):
            raise PreflightError("Navigator WorkFlow targets do not match host repository targets")
        workflow_target_ids.add(repository_id)
    if workflow_target_ids != set(targets_by_id):
        raise PreflightError(
            "Navigator WorkFlow must include the exact ten host repository targets"
        )
    rerun_actions = [
        action
        for action in allowed_actions
        if isinstance(action, dict) and action.get("id") == "rerun_source_ci"
    ]
    expected_target_ids = set(expected_provider_map)
    expected_workflow_ids = sorted(all_workflow_ids)
    if len(rerun_actions) != 1:
        raise PreflightError(
            "Navigator WorkFlow rerun grant differs from the verified provider allowlist"
        )
    rerun_action = rerun_actions[0]
    workflow_target_ids = rerun_action.get("targetIds")
    if (
        not isinstance(workflow_target_ids, list)
        or any(not isinstance(item, str) for item in workflow_target_ids)
        or len(set(workflow_target_ids)) != len(workflow_target_ids)
    ):
        raise PreflightError("Navigator WorkFlow rerun action has invalid target IDs")
    if (
        rerun_action.get("action") != "rerun_source_ci"
        or set(workflow_target_ids) != expected_target_ids
        or rerun_action.get("sourceWorkflowIds") != expected_workflow_ids
        or rerun_action.get("branchPrefixes") != ["cyrene/assistant/"]
        or rerun_action.get("baseBranches") != []
        or rerun_action.get("pathPrefixes") != []
        or rerun_action.get("issueNumbers") != []
        or rerun_action.get("permittedOperations") != ["github.actions.rerun_failed"]
    ):
        raise PreflightError(
            "Navigator WorkFlow rerun grant differs from the verified provider allowlist"
        )


def _validate_workflow_definition_disabled(workflow: dict[str, Any]) -> None:
    """Require owner acceptance before a scheduled WorkFlow definition can run."""

    if workflow.get("enabled") is not False:
        raise PreflightError(
            "Navigator WorkFlow definition must remain disabled until real acceptance"
        )


def _validate_workflow_model_policy(compose: dict[str, Any], workflow: dict[str, Any]) -> None:
    """Require ACP workflow metadata to describe its native model without fallback."""

    navigator = compose.get("services", {}).get("navigator", {})
    environment = navigator.get("environment", {}) if isinstance(navigator, dict) else {}
    if environment.get("CYRENE_HARNESS_EXECUTION_BACKEND") != "codebuddy-acp":
        return
    expected = {
        "preferredProvider": "codebuddy",
        "preferredModel": environment.get("CYRENE_HARNESS_CODEBUDDY_ACP_MODEL"),
        "fallback": "configured_no_additional_spend",
    }
    if workflow.get("modelPolicy") != expected:
        raise PreflightError(
            "CodeBuddy ACP WorkFlow modelPolicy must use its configured native model and no-spend metadata"
        )


def _validate_optional_workflow_definition_disabled(config_dir: Path) -> None:
    """Check a present private workflow file without requiring stage-1 target inventory."""

    path = config_dir.resolve().parent / "proactive-assistant-workflow.v2.json"
    if not path.exists():
        return
    if (
        path.is_symlink()
        or not path.is_file()
        or not _private_artifact(path)
        or path.stat().st_mode & 0o077
    ):
        raise PreflightError("Navigator WorkFlow definition must be a private owner-only file")
    try:
        workflow = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError("Navigator WorkFlow definition is invalid JSON") from error
    if not isinstance(workflow, dict):
        raise PreflightError("Navigator WorkFlow definition must be a JSON object")
    _validate_workflow_definition_disabled(workflow)


def _validate_cloud_acceptance_mount(service: dict[str, Any]) -> None:
    """Require only the private acceptance ledger directory in the provider container."""

    target = "/run/cyrene/.artifacts/teams-cloud-20261023/acceptance"
    matches = [
        row
        for row in service.get("volumes", [])
        if isinstance(row, dict) and row.get("target") == target
    ]
    if len(matches) != 1:
        raise PreflightError("Cloud Management must mount one private acceptance workspace")
    volume = matches[0]
    source = Path(volume.get("source", ""))
    if (
        volume.get("type") != "bind"
        or volume.get("read_only") is True
        or not _private_artifact(source)
        or source.is_symlink()
        or not source.is_dir()
        or source.stat().st_uid != 10001
        or source.stat().st_mode & 0o077
    ):
        raise PreflightError(
            "Cloud Management acceptance directory must stay owner-only under private .artifacts"
        )


def _service_secret_targets(service: dict[str, Any]) -> set[str]:
    """Return mounted Compose secret targets without reading their contents."""

    return {
        row["target"]
        for row in service.get("secrets", [])
        if isinstance(row, dict) and isinstance(row.get("target"), str)
    }


def _validate_internal_https_topology(config: dict[str, Any]) -> None:
    """Require HTTPS URLs, endpoint TLS files, and CA mounts for the private HTTP APIs."""

    services = config.get("services", {})
    navigator = services.get("navigator", {})
    persistence = services.get("navigator-persistence", {})
    exchange = services.get("exchange", {})
    navigator_env = navigator.get("environment", {})
    persistence_env = persistence.get("environment", {})
    exchange_env = exchange.get("environment", {})
    if (
        navigator_env.get("CYRENE_PERSISTENCE_URL") != "https://navigator-persistence:8080"
        or navigator_env.get("CYRENE_EXCHANGE_CONNECTOR_INTERNAL_URL") != "https://exchange:8081"
    ):
        raise PreflightError("Navigator private persistence and Exchange APIs must use HTTPS")
    if (
        navigator_env.get("NODE_EXTRA_CA_CERTS") != "/run/secrets/internal-service-root-ca"
        or navigator_env.get("CYRENE_INTERNAL_CA_FILE") != "/run/secrets/internal-service-root-ca"
        or "internal-service-root-ca" not in _service_secret_targets(navigator)
    ):
        raise PreflightError(
            "Navigator must trust the private service CA without replacing public roots"
        )
    endpoint_paths = {
        "exchange": {
            "environment": exchange_env,
            "certificate": "CYRENE_EXCHANGE_TLS_CERT_FILE",
            "key": "CYRENE_EXCHANGE_TLS_KEY_FILE",
            "cert_path": "/run/secrets/exchange-tls-cert",
            "key_path": "/run/secrets/exchange-tls-key",
            "targets": {"exchange-tls-cert", "exchange-tls-key", "internal-service-root-ca"},
            "health_url": "https://exchange:8081/readyz",
        },
        "navigator persistence": {
            "environment": persistence_env,
            "certificate": "CYRENE_PERSISTENCE_TLS_CERT_FILE",
            "key": "CYRENE_PERSISTENCE_TLS_KEY_FILE",
            "cert_path": "/run/secrets/navigator-persistence-tls-cert",
            "key_path": "/run/secrets/navigator-persistence-tls-key",
            "targets": {
                "navigator-persistence-tls-cert",
                "navigator-persistence-tls-key",
                "internal-service-root-ca",
            },
            "health_url": "https://navigator-persistence:8080/readyz",
        },
    }
    for label, expected in endpoint_paths.items():
        environment = expected["environment"]
        if (
            environment.get(expected["certificate"]) != expected["cert_path"]
            or environment.get(expected["key"]) != expected["key_path"]
            or not expected["targets"]
            <= _service_secret_targets(
                services.get("exchange" if label == "exchange" else "navigator-persistence", {})
            )
        ):
            raise PreflightError(f"{label} HTTPS listener lacks its private certificate mounts")
        healthcheck = services.get(
            "exchange" if label == "exchange" else "navigator-persistence", {}
        ).get("healthcheck", {})
        test = healthcheck.get("test", [])
        if expected["health_url"] not in " ".join(
            str(part) for part in test
        ) or "/run/secrets/internal-service-root-ca" not in " ".join(str(part) for part in test):
            raise PreflightError(f"{label} readiness probe must verify HTTPS and the private CA")


def _validate_internal_service_certificates(secrets_dir: Path) -> None:
    """Verify private HTTPS leaf certificates, key pairs, trust chain, and SANs."""

    root = secrets_dir / "internal-service-root-ca"
    for prefix, hostname in (
        ("exchange", "exchange"),
        ("navigator-persistence", "navigator-persistence"),
    ):
        certificate = secrets_dir / f"{prefix}-tls-cert"
        private_key = secrets_dir / f"{prefix}-tls-key"
        _run(["openssl", "verify", "-CAfile", str(root), str(certificate)])
        _run(["openssl", "x509", "-in", str(certificate), "-noout", "-checkhost", hostname])
        subject_alt_names = _run(
            ["openssl", "x509", "-in", str(certificate), "-noout", "-ext", "subjectAltName"]
        )
        if f"DNS:{hostname}" not in subject_alt_names:
            raise PreflightError(f"{prefix} HTTPS certificate lacks its service DNS SAN")
        certificate_public_key = _run(
            ["openssl", "x509", "-in", str(certificate), "-pubkey", "-noout"]
        )
        private_key_public_key = _run(["openssl", "pkey", "-in", str(private_key), "-pubout"])
        if certificate_public_key != private_key_public_key:
            raise PreflightError(f"{prefix} TLS certificate and private key do not match")


def _validate_computer_proxy_certificate(config: dict[str, Any], secrets_dir: Path) -> None:
    """Verify the Caddy proxy leaf certificate, key pair, and Exchange-facing DNS SAN."""

    if "computer-runtime-proxy" not in config.get("services", {}):
        return
    certificate = secrets_dir / "computer-runtime-proxy-server-cert"
    private_key = secrets_dir / "computer-runtime-proxy-server-key"
    root = secrets_dir / "exchange-plugin-root-ca"
    _run(["openssl", "verify", "-CAfile", str(root), str(certificate)])
    _run(
        [
            "openssl",
            "x509",
            "-in",
            str(certificate),
            "-noout",
            "-checkhost",
            "computer-runtime-proxy",
        ]
    )
    subject_alt_names = _run(
        ["openssl", "x509", "-in", str(certificate), "-noout", "-ext", "subjectAltName"]
    )
    if "DNS:computer-runtime-proxy" not in subject_alt_names:
        raise PreflightError("computer proxy certificate lacks its Exchange-facing DNS SAN")
    certificate_public_key = _run(["openssl", "x509", "-in", str(certificate), "-pubkey", "-noout"])
    private_key_public_key = _run(["openssl", "pkey", "-in", str(private_key), "-pubout"])
    if certificate_public_key != private_key_public_key:
        raise PreflightError("computer proxy TLS certificate and private key do not match")


def _validate_exchange_authority_identity(config: dict[str, Any], secrets_dir: Path) -> None:
    """Bind Exchange's mounted Authority client certificate to the configured pin."""

    authority_environment = (
        config.get("services", {}).get("workspace-authority", {}).get("environment", {})
    )
    configured = authority_environment.get("CYRENE_AUTHORITY_EXCHANGE_MTLS_SHA256", "")
    if not isinstance(configured, str):
        raise PreflightError("Authority Exchange mTLS fingerprints must be SHA-256 hex values")
    pins = [value.strip().lower() for value in configured.split(",") if value.strip()]
    if not pins or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in pins):
        raise PreflightError("Authority Exchange mTLS fingerprints must be SHA-256 hex values")

    certificate = secrets_dir / "exchange-authority-client-cert"
    private_key = secrets_dir / "exchange-authority-client-key"
    client_ca = secrets_dir / "authority-tls-client-ca"
    _run(["openssl", "verify", "-CAfile", str(client_ca), str(certificate)])
    certificate_public_key = _run(["openssl", "x509", "-in", str(certificate), "-pubkey", "-noout"])
    private_key_public_key = _run(["openssl", "pkey", "-in", str(private_key), "-pubout"])
    if certificate_public_key != private_key_public_key:
        raise PreflightError("Exchange Authority client certificate and key do not match")
    try:
        der_result = subprocess.run(
            ["openssl", "x509", "-in", str(certificate), "-outform", "DER"],
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise PreflightError(
            "Exchange Authority certificate fingerprint could not be computed"
        ) from error
    if der_result.returncode != 0 or not der_result.stdout:
        raise PreflightError("Exchange Authority certificate fingerprint could not be computed")
    observed = hashlib.sha256(der_result.stdout).hexdigest()
    if observed not in pins:
        raise PreflightError(
            "Authority Exchange mTLS pin does not match the mounted Exchange client certificate"
        )


def _read_private_receipt(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    """Load one owner-only acceptance receipt without exposing its contents."""

    if (
        not _private_artifact(path)
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != 10001
        or path.stat().st_mode & 0o077
    ):
        raise PreflightError(f"{label} must be a 10001-owned owner-only receipt under .artifacts")
    raw = path.read_bytes()
    try:
        document = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError(f"{label} is invalid JSON") from error
    if not isinstance(document, dict):
        raise PreflightError(f"{label} must be a JSON object")
    return document, raw


def _validate_receipt_time(value: Any, label: str) -> None:
    """Require an explicit timezone on one acceptance timestamp."""

    if not isinstance(value, str) or not value:
        raise PreflightError(f"{label} requires an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise PreflightError(f"{label} has an invalid ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise PreflightError(f"{label} timestamp must include a timezone")


def _validate_github_hf_receipt(path: Path, expected_owner: str) -> dict[str, Any]:
    """Require real GitHub and Hugging Face private-write-clean receipts."""

    evidence, raw = _read_private_receipt(path, "GitHub/Hugging Face acceptance evidence")
    task_id = evidence.get("taskId")
    providers = evidence.get("providers")
    if (
        frozenset(evidence)
        not in {
            frozenset(
                {
                    "schemaVersion",
                    "stage",
                    "status",
                    "evidenceClass",
                    "verifiedAt",
                    "taskId",
                    "owner",
                    "reservedUsd",
                    "cumulativeReservedUsd",
                    "providers",
                }
            ),
            frozenset(
                {
                    "$schema",
                    "schemaVersion",
                    "stage",
                    "status",
                    "evidenceClass",
                    "verifiedAt",
                    "taskId",
                    "owner",
                    "reservedUsd",
                    "cumulativeReservedUsd",
                    "providers",
                }
            ),
        }
        or evidence.get("schemaVersion") != "cyrene-cloud-acceptance-evidence-v1"
        or not isinstance(providers, dict)
        or set(providers) != {"github", "huggingface"}
        or any(
            not isinstance(result, dict)
            or set(result)
            != {
                "identityStatus",
                "privateReadStatus",
                "writeReadbackStatus",
                "cleanupReadbackStatus",
                "resourceRef",
                "writeReadbackSha256",
                "cleanupReadbackAt",
            }
            for result in providers.values()
        )
        or evidence.get("stage") != "github_hf"
        or evidence.get("status") != "VERIFIED"
        or evidence.get("evidenceClass") != "REAL_PROVIDER_API"
        or evidence.get("owner") != expected_owner
        or not isinstance(task_id, str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{2,43}", task_id)
        or evidence.get("reservedUsd") != 0
        or evidence.get("cumulativeReservedUsd") != 0
        or PLACEHOLDER_RE.search(json.dumps(evidence, sort_keys=True))
    ):
        raise PreflightError(
            "GitHub/Hugging Face evidence must prove real provider API checks for the same owner"
        )
    _validate_receipt_time(evidence.get("verifiedAt"), "GitHub/Hugging Face evidence")
    for provider, result in providers.items():
        if (
            not isinstance(result, dict)
            or result.get("identityStatus") != "VERIFIED"
            or result.get("privateReadStatus") != "VERIFIED"
            or result.get("writeReadbackStatus") != "VERIFIED"
            or result.get("cleanupReadbackStatus") != "ABSENT"
            or not isinstance(result.get("resourceRef"), str)
            or not result["resourceRef"].strip()
            or not isinstance(result.get("writeReadbackSha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", result["writeReadbackSha256"])
        ):
            raise PreflightError(
                f"{provider} evidence must contain identity, private read, write/read-back, and cleanup proof"
            )
        _validate_receipt_time(result.get("cleanupReadbackAt"), f"{provider} cleanup read-back")
    return {**evidence, "_raw": raw}


def _validate_cloud_acceptance_ledger(
    path: Path,
    *,
    expected_stage: str,
    expected_owner: str,
    expected_task_id: str,
    previous_evidence: bytes,
) -> dict[str, Any]:
    """Require a real CLI ledger whose every created resource was deleted and re-read absent."""

    label = f"{expected_stage} acceptance ledger"
    ledger, raw = _read_private_receipt(path, label)
    expected_providers = {"azure", "gcp"} if expected_stage == "azure_gcp" else {"runpod"}
    resources = ledger.get("resources")
    if (
        ledger.get("schemaVersion") != "cyrene-cloud-acceptance-ledger-v1"
        or ledger.get("stage") != expected_stage
        or ledger.get("status") != "VERIFIED"
        or ledger.get("evidenceClass") != "REAL_VENDOR_CLI"
        or ledger.get("owner") != expected_owner
        or ledger.get("taskId") != expected_task_id
        or ledger.get("budgetUsd") != 5.0
        or ledger.get("previousEvidenceSha256") != hashlib.sha256(previous_evidence).hexdigest()
        or not isinstance(resources, dict)
        or not resources
        or not isinstance(ledger.get("reservedUsd"), (int, float))
        or isinstance(ledger.get("reservedUsd"), bool)
        or not math.isfinite(ledger["reservedUsd"])
        or not isinstance(ledger.get("cumulativeReservedUsd"), (int, float))
        or isinstance(ledger.get("cumulativeReservedUsd"), bool)
        or not math.isfinite(ledger["cumulativeReservedUsd"])
        or ledger["reservedUsd"] < 0
        or ledger["cumulativeReservedUsd"] > 5.0
        or PLACEHOLDER_RE.search(json.dumps(ledger, sort_keys=True))
    ):
        raise PreflightError(f"{label} is not a verified, same-task real provider ledger")
    _validate_receipt_time(ledger.get("verifiedAt"), f"{label} verification")
    _validate_receipt_time(ledger.get("cleanupVerifiedAt"), f"{label} cleanup")
    observed_providers: set[str] = set()
    for resource_id, resource in resources.items():
        ownership = resource.get("ownership") if isinstance(resource, dict) else None
        events = resource.get("events") if isinstance(resource, dict) else None
        absence_verified = isinstance(events, list) and any(
            isinstance(event, dict)
            and event.get("action") == "verify-cleanup-absence"
            and event.get("outcome") == "EXITED"
            and isinstance(event.get("exitCode"), int)
            and not isinstance(event.get("exitCode"), bool)
            and event.get("exitCode") == 0
            for event in events
        )
        if (
            not isinstance(resource_id, str)
            or not isinstance(resource, dict)
            or resource.get("state") != "DELETED"
            or not isinstance(ownership, dict)
            or ownership.get("taskId") != expected_task_id
            or ownership.get("owner") != expected_owner
            or not isinstance(ownership.get("marker"), str)
            or expected_task_id not in ownership["marker"]
            or not isinstance(resource.get("provider"), str)
            or not absence_verified
        ):
            raise PreflightError(f"{label} contains a resource without verified absence read-back")
        observed_providers.add(resource.get("provider"))
    if observed_providers != expected_providers:
        raise PreflightError(
            f"{label} must include exactly {sorted(expected_providers)} provider resources"
        )
    return {**ledger, "_raw": raw}


def _validate_cloud_api_evidence(
    path: Path,
    *,
    expected_stage: str,
    expected_providers: set[str],
    expected_owner: str,
    expected_task_id: str,
    previous_evidence: bytes,
    previous_reserved_usd: float,
) -> dict[str, Any]:
    """Require DSH-to-Exchange-to-provider API proof and verified resource cleanup."""

    label = f"{expected_stage} provider API evidence"
    evidence, raw = _read_private_receipt(path, label)
    providers = evidence.get("providers")
    reserved = evidence.get("reservedUsd")
    cumulative = evidence.get("cumulativeReservedUsd")
    if (
        set(evidence)
        != {
            "schemaVersion",
            "taskId",
            "owner",
            "stage",
            "status",
            "evidenceClass",
            "executionPath",
            "verifiedAt",
            "reservedUsd",
            "cumulativeReservedUsd",
            "previousEvidenceSha256",
            "providers",
        }
        or evidence.get("schemaVersion") != "cyrene-cloud-api-evidence-v1"
        or evidence.get("taskId") != expected_task_id
        or evidence.get("owner") != expected_owner
        or evidence.get("stage") != expected_stage
        or evidence.get("status") != "VERIFIED"
        or evidence.get("evidenceClass") != "REAL_PROVIDER_API"
        or evidence.get("executionPath") != "navigator_dsh_exchange_directplugin_api"
        or evidence.get("previousEvidenceSha256") != hashlib.sha256(previous_evidence).hexdigest()
        or not isinstance(reserved, (int, float))
        or isinstance(reserved, bool)
        or not math.isfinite(reserved)
        or reserved < 0
        or not isinstance(cumulative, (int, float))
        or isinstance(cumulative, bool)
        or not math.isfinite(cumulative)
        or cumulative < 0
        or cumulative > 5.0
        or not math.isclose(
            cumulative,
            previous_reserved_usd + reserved,
            rel_tol=0,
            abs_tol=0.000001,
        )
        or not isinstance(providers, list)
        or not providers
        or PLACEHOLDER_RE.search(json.dumps(evidence, sort_keys=True))
    ):
        raise PreflightError(
            f"{label} must be a closed, same-task REAL_PROVIDER_API receipt within the cumulative USD 5 limit"
        )
    _validate_receipt_time(evidence.get("verifiedAt"), label)

    observed_providers: set[str] = set()
    provider_fields = {
        "providerId",
        "identityStatus",
        "authMode",
        "profileScopeDigestSha256",
        "resources",
        "writeOperations",
    }
    resource_fields = {
        "ownership",
        "vendorRef",
        "desiredState",
        "actualState",
        "readiness",
        "cleanupAbsenceStatus",
        "cleanupReadbackAt",
    }
    operation_fields = {
        "toolId",
        "operationId",
        "requestDigestSha256",
        "resourceRef",
        "grantRef",
        "workOperationId",
        "outcome",
        "operationRef",
        "stateReadback",
        "resultDigestSha256",
    }
    for provider in providers:
        if not isinstance(provider, dict) or set(provider) != provider_fields:
            raise PreflightError(f"{label} provider records must match the closed schema")
        provider_id = provider.get("providerId")
        if (
            not isinstance(provider_id, str)
            or provider_id not in expected_providers
            or provider_id in observed_providers
            or provider.get("identityStatus") != "VERIFIED"
            or not isinstance(provider.get("authMode"), str)
            or not provider["authMode"]
            or not isinstance(provider.get("profileScopeDigestSha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", provider["profileScopeDigestSha256"])
        ):
            raise PreflightError(f"{label} provider identity or profile scope is not verified")
        observed_providers.add(provider_id)
        resources = provider.get("resources")
        operations = provider.get("writeOperations")
        if not isinstance(resources, list) or not resources or not isinstance(operations, list):
            raise PreflightError(f"{label} requires resource and formal write-operation records")
        for resource in resources:
            if not isinstance(resource, dict) or set(resource) != resource_fields:
                raise PreflightError(f"{label} resource records must match the closed schema")
            ownership = resource.get("ownership")
            if (
                not isinstance(ownership, dict)
                or set(ownership) != {"taskId", "owner", "marker"}
                or ownership.get("taskId") != expected_task_id
                or ownership.get("owner") != expected_owner
                or not isinstance(ownership.get("marker"), str)
                or expected_task_id not in ownership["marker"]
                or not isinstance(resource.get("vendorRef"), str)
                or not resource["vendorRef"]
                or any(
                    not isinstance(resource.get(field), str) or not resource[field]
                    for field in ("desiredState", "actualState", "readiness")
                )
                or resource.get("cleanupAbsenceStatus") != "ABSENT"
            ):
                raise PreflightError(
                    f"{label} resource is missing task ownership or verified cleanup absence"
                )
            _validate_receipt_time(resource.get("cleanupReadbackAt"), f"{label} cleanup read-back")
        if not operations:
            raise PreflightError(f"{label} must contain at least one successful provider API write")
        for operation in operations:
            if not isinstance(operation, dict) or set(operation) != operation_fields:
                raise PreflightError(
                    f"{label} write-operation records must match the closed schema"
                )
            state_readback = operation.get("stateReadback")
            if (
                any(
                    not isinstance(operation.get(field), str) or not operation[field]
                    for field in (
                        "toolId",
                        "operationId",
                        "resourceRef",
                        "grantRef",
                        "workOperationId",
                        "operationRef",
                    )
                )
                or any(
                    not isinstance(operation.get(field), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", operation[field])
                    for field in ("requestDigestSha256", "resultDigestSha256")
                )
                or operation.get("outcome") != "SUCCEEDED"
                or not isinstance(state_readback, dict)
                or set(state_readback) != {"status", "observedAt", "stateDigestSha256"}
                or state_readback.get("status") != "VERIFIED"
                or not isinstance(state_readback.get("stateDigestSha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", state_readback["stateDigestSha256"])
            ):
                raise PreflightError(f"{label} operation lacks a successful typed read-back")
            _validate_receipt_time(state_readback.get("observedAt"), f"{label} operation read-back")
    if observed_providers != expected_providers:
        raise PreflightError(f"{label} must include exactly {sorted(expected_providers)} providers")
    return {**evidence, "_raw": raw}


def _validate_stage_acceptance_receipts(
    *,
    mode: str,
    config: dict[str, Any],
    github_hf_evidence: Path | None,
    azure_gcp_ledger: Path | None,
    runpod_ledger: Path | None,
    azure_gcp_api_evidence: Path | None = None,
    runpod_api_evidence: Path | None = None,
) -> None:
    """Gate optional cloud profiles on verified prior-stage evidence and cleanup."""

    if mode == "connector-auth-readonly":
        # Direct identity probes are checked separately; this mode never claims lifecycle writes.
        return
    services = config.get("services", {})
    operator = services.get("directory-operator", {}).get("environment", {})
    expected_owner = operator.get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID")
    if azure_gcp_ledger is not None or runpod_ledger is not None:
        raise PreflightError(
            "this task reserves billable lifecycle writes for the DSH API path; vendor-CLI mutation ledgers are not accepted"
        )
    cloud = services.get("cloud-management-phase2")
    runpod_enabled = isinstance(cloud, dict) and "runpod" in cloud.get("profiles", [])
    cloud_enabled = isinstance(cloud, dict)
    require_stage1 = mode in {"stage2", "stage3", "cutover"}
    if not require_stage1 and mode not in {"staging", "stage1"}:
        require_stage1 = cloud_enabled
    stage1: dict[str, Any] | None = None
    stage1_bytes: bytes | None = None
    if require_stage1:
        if github_hf_evidence is None:
            raise PreflightError("this phase requires verified GitHub/Hugging Face evidence")
        stage1 = _validate_github_hf_receipt(github_hf_evidence.resolve(), expected_owner)
        stage1_bytes = stage1["_raw"]

    require_stage2 = mode in {"stage3"} or (mode == "cutover" and cloud_enabled)
    stage2_api: dict[str, Any] | None = None
    if require_stage2:
        if stage1 is None or stage1_bytes is None or azure_gcp_api_evidence is None:
            raise PreflightError("this phase requires verified Azure/GCP provider API evidence")
        stage2_api = _validate_cloud_api_evidence(
            azure_gcp_api_evidence.resolve(),
            expected_stage="azure_gcp",
            expected_providers={"azure", "gcp"},
            expected_owner=expected_owner,
            expected_task_id=stage1["taskId"],
            previous_evidence=stage1_bytes,
            previous_reserved_usd=0.0,
        )

    require_stage3 = mode == "cutover" and runpod_enabled
    if require_stage3:
        if stage2_api is None or runpod_api_evidence is None:
            raise PreflightError(
                "RunPod cutover requires verified RunPod API evidence and its cleanup absence proof"
            )
        _validate_cloud_api_evidence(
            runpod_api_evidence.resolve(),
            expected_stage="runpod",
            expected_providers={"runpod"},
            expected_owner=expected_owner,
            expected_task_id=stage2_api["taskId"],
            previous_evidence=stage2_api["_raw"],
            previous_reserved_usd=float(stage2_api["cumulativeReservedUsd"]),
        )


def _validate_tailscale() -> None:
    """Require the existing host Tailscale daemon to remain online."""

    _run(["systemctl", "is-active", "--quiet", "tailscaled"])
    try:
        state = json.loads(_run(["tailscale", "status", "--json"]))
    except json.JSONDecodeError as error:
        raise PreflightError("Tailscale status is not valid JSON") from error
    self_node = state.get("Self") if isinstance(state, dict) else None
    if (
        not isinstance(self_node, dict)
        or not self_node.get("ID")
        or state.get("BackendState") != "Running"
    ):
        raise PreflightError(
            "Tailscale is not connected; preserve and repair the existing App Connector first"
        )


def _validate_host(required_free_gib: float) -> tuple[int, float]:
    """Recognize the 4 GiB VM class and require native ARM64, two CPUs, and disk headroom."""

    if platform.system() != "Linux" or platform.machine().lower() not in {"aarch64", "arm64"}:
        raise PreflightError("production host must be native Linux ARM64")
    memory_mib = _parse_meminfo()
    if memory_mib < MIN_GUEST_MEMORY_MIB:
        raise PreflightError(
            f"host has {memory_mib} MiB guest RAM; at least {MIN_GUEST_MEMORY_MIB} MiB is required"
        )
    if (os.cpu_count() or 0) < 2:
        raise PreflightError(
            "host must have at least two CPUs for the one-task concurrency profile"
        )
    free_gib = shutil.disk_usage("/").free / GIB
    if free_gib < required_free_gib:
        raise PreflightError(
            f"host root filesystem has {free_gib:.2f} GiB free; {required_free_gib:.2f} GiB is required"
        )
    _validate_tailscale()
    return memory_mib, free_gib


def _validate_owned_staging_ingress(
    config: dict[str, Any],
    image_lock: dict[str, Any],
    docker_prefix: list[str],
    mode: str,
) -> str:
    """Verify the one explicitly owned loopback ingress binding for a canary."""

    if mode not in {"stage1", "staging", "connector-auth-readonly"}:
        raise PreflightError("owned loopback staging ingress is unavailable in this mode")
    project = config.get("name")
    service = config.get("services", {}).get("teams-public-ingress", {})
    ports = service.get("ports", []) if isinstance(service, dict) else []
    if (
        not isinstance(project, str)
        or not project
        or not isinstance(ports, list)
        or len(ports) != 1
        or not isinstance(ports[0], dict)
        or ports[0].get("target") != 443
        or str(ports[0].get("published")) != "18443"
        or ports[0].get("host_ip") != "127.0.0.1"
        or ports[0].get("protocol", "tcp") != "tcp"
    ):
        raise PreflightError("owned staging ingress must publish only 127.0.0.1:18443/tcp")

    image_ref = service.get("image")
    image_rows = [
        row
        for row in image_lock.get("images", [])
        if isinstance(row, dict) and row.get("image") == image_ref
    ]
    image_rows.extend(
        row
        for row in image_lock.get("externalImages", {}).values()
        if isinstance(row, dict) and row.get("localImage") == image_ref
    )
    if len(image_rows) != 1 or not isinstance(image_rows[0].get("imageId"), str):
        raise PreflightError("owned staging ingress image is absent from the exact image lock")

    container_ids = _run(
        [
            *docker_prefix,
            "ps",
            "--filter",
            "publish=18443",
            "--format",
            "{{.ID}}",
        ]
    ).splitlines()
    container_ids = [value.strip() for value in container_ids if value.strip()]
    if len(container_ids) != 1 or re.fullmatch(r"[0-9a-f]{12,64}", container_ids[0]) is None:
        raise PreflightError("loopback port 18443 must have exactly one running Docker owner")
    try:
        inspected = json.loads(
            _run([*docker_prefix, "inspect", container_ids[0]], capture=True),
            object_pairs_hook=_unique_json_object,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise PreflightError("Docker returned invalid staging ingress inspection data") from error
    if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise PreflightError("Docker staging ingress ownership readback is ambiguous")
    container = inspected[0]
    container_config = container.get("Config")
    host_config = container.get("HostConfig")
    labels = container_config.get("Labels") if isinstance(container_config, dict) else None
    bindings = host_config.get("PortBindings") if isinstance(host_config, dict) else None
    entries = bindings.get("443/tcp") if isinstance(bindings, dict) else None
    state = container.get("State")
    if (
        not isinstance(labels, dict)
        or labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != "teams-public-ingress"
        or not isinstance(container_config, dict)
        or container_config.get("Image") != image_ref
        or container.get("Image") != image_rows[0].get("imageId")
        or not isinstance(state, dict)
        or state.get("Running") is not True
        or not isinstance(entries, list)
        or entries != [{"HostIp": "127.0.0.1", "HostPort": "18443"}]
        or set(bindings) != {"443/tcp"}
    ):
        raise PreflightError(
            "loopback port 18443 is not uniquely owned by the locked Compose ingress service"
        )
    return image_rows[0]["imageId"]


def _parse_listener_endpoints(listeners: str) -> list[tuple[str, str]]:
    """Parse every numeric TCP-listener local endpoint from ``ss -H -ltn``."""

    endpoints: list[tuple[str, str]] = []
    for line in listeners.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] != "LISTEN":
            raise PreflightError("host listener inventory contains an unknown row")
        host, separator, port = fields[3].rpartition(":")
        if not separator or not port.isdigit():
            raise PreflightError("host listener inventory contains an unknown local endpoint")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if not host:
            raise PreflightError("host listener inventory contains an empty local address")
        endpoints.append((host, port))
    return endpoints


def _validate_host_ports(
    config: dict[str, Any],
    *,
    mode: str | None = None,
    docker_prefix: list[str] | None = None,
    image_lock: dict[str, Any] | None = None,
    allow_owned_staging_ingress: bool = False,
    ingress_exposure: str = "public-cutover",
) -> str | None:
    """Refuse occupied ports unless the explicit Stage 1 owner check passes."""

    owned_staging_image_id = None
    host_fronted_staging = (
        mode == "connector-auth-readonly" and ingress_exposure == "host-fronted-staging"
    )
    if host_fronted_staging:
        if image_lock is None or docker_prefix is None:
            raise PreflightError(
                "host-fronted ingress requires its exact image lock and Docker readback"
            )
        owned_staging_image_id = _validate_owned_staging_ingress(
            config, image_lock, docker_prefix, mode
        )
    elif allow_owned_staging_ingress:
        if mode not in {"stage1", "staging"} or image_lock is None or docker_prefix is None:
            raise PreflightError(
                "owned staging ingress requires Stage 1 mode, the exact image lock, and Docker"
            )
        owned_staging_image_id = _validate_owned_staging_ingress(
            config, image_lock, docker_prefix, mode
        )

    service = config["services"].get("teams-public-ingress", {})
    ports = service.get("ports", [])
    if not ports:
        return
    if mode == "connector-auth-readonly":
        if host_fronted_staging:
            listeners = _run(["ss", "-H", "-ltn"])
            staging_listeners = [
                host for host, port in _parse_listener_endpoints(listeners) if port == "18443"
            ]
            if staging_listeners != ["127.0.0.1"]:
                raise PreflightError(
                    "host-fronted ingress requires exactly one loopback-only 127.0.0.1:18443 listener"
                )
            return owned_staging_image_id
        # The stage-transition planner verifies the exact active public ingress owner and port
        # before apply; the existing listener is expected to be occupied during its read-only plan.
        return None
    listeners = _run(["ss", "-H", "-ltn"])
    if allow_owned_staging_ingress:
        staging_listeners = [
            host for host, port in _parse_listener_endpoints(listeners) if port == "18443"
        ]
        if any(host != "127.0.0.1" for host in staging_listeners) or len(staging_listeners) > 1:
            raise PreflightError(
                "loopback port 18443 has a wildcard, non-loopback, or ambiguous host listener"
            )
    for row in ports:
        published = str(row.get("published", ""))
        if published and re.search(rf":{re.escape(published)}(?:\s|$)", listeners):
            if (
                allow_owned_staging_ingress
                and published == "18443"
                and row.get("host_ip") == "127.0.0.1"
            ):
                continue
            raise PreflightError(
                f"host port {published} is already occupied; preserve the current ingress and plan a cutover"
            )
    return owned_staging_image_id


_DOCKER_MEMORY_UNIT_BYTES = {
    "B": 1,
    "kB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}


def _docker_memory_usage_mib(value: str) -> float:
    """Parse only the usage side of Docker's human-readable per-container memory stat."""

    match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*/", value)
    if not match or match.group(2) not in _DOCKER_MEMORY_UNIT_BYTES:
        raise PreflightError("Docker did not return a parseable per-service memory statistic")
    return float(match.group(1)) * _DOCKER_MEMORY_UNIT_BYTES[match.group(2)] / MIB


_STAGE1_REQUIRED_SERVICE_NAMES = frozenset(
    {
        "postgres",
        "workspace-authority",
        "account-center",
        "navigator-persistence",
        "navigator",
        "exchange",
        "github-issues",
        "teams-provider",
        "teams-ingress",
        "teams-public-ingress",
    }
)
_STAGE1_OPTIONAL_SERVICE_NAMES = frozenset(
    {"huggingface", "computer-runtime", "computer-runtime-proxy"}
)
_STAGE1_ALLOWED_SERVICE_NAMES = _STAGE1_REQUIRED_SERVICE_NAMES | _STAGE1_OPTIONAL_SERVICE_NAMES
_COMPUTER_RUNTIME_SERVICE_NAMES = frozenset({"computer-runtime", "computer-runtime-proxy"})


def _expected_stage1_services(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Select Stage 1 runtime services from the exact rendered Compose profile set.

    Runtime services without a profile are active; the Stage 1 `public-ingress`
    profile is also required. One-shot jobs and every later-stage profile are excluded.
    中文：保留基础服务及 Stage 1 公网入口，排除初始化任务和后续阶段 profile。
    """

    services = config.get("services")
    if not isinstance(services, dict):
        raise PreflightError("rendered Compose configuration has no valid services")
    expected: dict[str, dict[str, Any]] = {}
    for name, service in services.items():
        if not isinstance(name, str) or not isinstance(service, dict):
            raise PreflightError("rendered Compose service inventory is malformed")
        if service.get("restart") == "no":
            continue
        profiles = service.get("profiles", [])
        if not isinstance(profiles, list) or any(
            not isinstance(profile, str) for profile in profiles
        ):
            raise PreflightError("rendered Compose service profiles are malformed")
        if profiles and "public-ingress" not in profiles:
            continue
        if name not in _STAGE1_ALLOWED_SERVICE_NAMES:
            raise PreflightError(f"unsupported active Stage 1 Compose service: {name}")
        expected[name] = service
    missing = sorted(_STAGE1_REQUIRED_SERVICE_NAMES - set(expected))
    if missing:
        raise PreflightError("Stage 1 Compose services are missing: " + ", ".join(missing))
    computer_services = set(expected) & _COMPUTER_RUNTIME_SERVICE_NAMES
    if computer_services and computer_services != _COMPUTER_RUNTIME_SERVICE_NAMES:
        raise PreflightError("Stage 1 computer runtime and proxy must be configured together")
    return expected


def _validate_cutover_runtime(
    config: dict[str, Any], docker_prefix: list[str], mode: str
) -> dict[str, float]:
    """Require staged services to be running/healthy and record live RSS before cutover."""

    services = config.get("services", {})
    if mode != "cutover":
        return {}
    active_profiles = {"public-ingress", "cloud-phase2", "runpod"}
    expected: dict[str, bool] = {}
    for name, service in services.items():
        profiles = set(service.get("profiles", [])) if isinstance(service, dict) else set()
        if isinstance(service, dict) and service.get("restart") == "no":
            continue
        if profiles and not profiles.intersection(active_profiles):
            continue
        if name == "cloud-management-phase2":
            active = "runpod" if "runpod" in profiles else "cloud-phase2"
            if active not in profiles:
                continue
        expected[name] = bool(service.get("healthcheck"))
    prefix = "cyrene-teams-cloud-"
    status_rows = _run([*docker_prefix, "ps", "--format", "{{.Names}}|{{.Status}}"]).splitlines()
    running: dict[str, str] = {}
    for line in status_rows:
        name, separator, status = line.partition("|")
        if separator and name.startswith(prefix):
            for service_name in expected:
                if name.startswith(f"{prefix}{service_name}-"):
                    running[service_name] = status
                    break
    missing = sorted(set(expected) - set(running))
    if missing:
        raise PreflightError(
            "staged Compose services must be running before public cutover: " + ", ".join(missing)
        )
    unhealthy = sorted(
        name
        for name, has_healthcheck in expected.items()
        if has_healthcheck and "(healthy)" not in running[name]
    )
    if unhealthy:
        raise PreflightError(
            "staged Compose services must pass their health checks before cutover: "
            + ", ".join(unhealthy)
        )

    stats_rows = _run(
        [*docker_prefix, "stats", "--no-stream", "--format", "{{.Name}}|{{.MemUsage}}"]
    ).splitlines()
    observed: dict[str, float] = {}
    for line in stats_rows:
        name, separator, usage = line.partition("|")
        if not separator or not name.startswith(prefix):
            continue
        for service_name in expected:
            if name.startswith(f"{prefix}{service_name}-"):
                observed[service_name] = _docker_memory_usage_mib(usage)
                break
    missing_stats = sorted(set(expected) - set(observed))
    if missing_stats:
        raise PreflightError(
            "Docker did not return staged service RSS for cutover: " + ", ".join(missing_stats)
        )
    return observed


def run_preflight(
    *,
    mode: str,
    compose_files: list[Path],
    env_files: list[Path],
    image_lock: Path,
    secrets_dir: Path,
    config_dir: Path,
    github_hf_evidence: Path | None,
    azure_gcp_ledger: Path | None,
    runpod_ledger: Path | None,
    azure_gcp_api_evidence: Path | None,
    runpod_api_evidence: Path | None,
    docker_prefix: list[str],
    max_memory_mib: int,
    max_cpus: float,
    required_free_gib: float,
    minimum_mem_available_mib: int,
    allow_owned_staging_ingress: bool = False,
    recovery_only: bool = False,
    connector_auth_phase: str | None = None,
    connector_auth_evidence: Path | None = None,
    connector_auth_task_id: str | None = None,
    huggingface_seed_file: Path | None = None,
    notification_anchor_file: Path | None = None,
    notification_provenance_file: Path | None = None,
    candidate_runpod_key_file: Path | None = None,
    ingress_exposure: str = "public-cutover",
    host_caddy_binary: str = "/usr/local/bin/caddy",
    openssl_binary: str = "openssl",
) -> dict[str, Any]:
    """Perform all read-only gates before the operator starts or cuts over services."""

    if mode not in {
        "bootstrap",
        "staging",
        "cutover",
        "stage1",
        "stage2",
        "stage3",
        "connector-auth-readonly",
    }:
        raise PreflightError(
            "mode must be bootstrap, staging, cutover, stage1, stage2, stage3, or connector-auth-readonly"
        )
    if ingress_exposure not in {"public-cutover", "host-fronted-staging"}:
        raise PreflightError("ingress exposure must be public-cutover or host-fronted-staging")
    if ingress_exposure == "host-fronted-staging" and mode != "connector-auth-readonly":
        raise PreflightError("host-fronted staging exposure is limited to connector-auth-readonly")
    if mode == "connector-auth-readonly":
        if connector_auth_phase not in CONNECTOR_AUTH_READONLY_PHASES:
            raise PreflightError(
                "connector auth phase must be provider-bootstrap or issued-connector-activation"
            )
        if connector_auth_evidence is None or not connector_auth_task_id:
            raise PreflightError("connector auth evidence and its exact task ID are required")
        if huggingface_seed_file is None:
            raise PreflightError("the exact candidate HF seed path is required")
        if connector_auth_phase == "provider-bootstrap" and (
            notification_anchor_file is not None or notification_provenance_file is not None
        ):
            raise PreflightError("provider bootstrap must not include notification anchor inputs")
        if connector_auth_phase == "issued-connector-activation" and (
            notification_anchor_file is None or notification_provenance_file is None
        ):
            raise PreflightError(
                "issued connector activation requires notification anchor/provenance"
            )
    elif any(
        value is not None
        for value in (
            connector_auth_phase,
            connector_auth_evidence,
            connector_auth_task_id,
            huggingface_seed_file,
            notification_anchor_file,
            notification_provenance_file,
            candidate_runpod_key_file,
        )
    ):
        raise PreflightError(
            "connector auth inputs are accepted only in connector-auth-readonly mode"
        )
    if allow_owned_staging_ingress and mode not in {"stage1", "staging"}:
        raise PreflightError("owned staging ingress cannot be used for bootstrap or cutover")
    if recovery_only and mode != "stage1":
        raise PreflightError("recovery-only validation is limited to the Stage 1 standalone stack")
    if not _private_artifact(image_lock) or any(not _private_artifact(path) for path in env_files):
        raise PreflightError(
            "image locks and environment files must be stored under umbrella .artifacts"
        )
    for env_file in env_files:
        if env_file.is_symlink() or not env_file.is_file() or env_file.stat().st_mode & 0o077:
            raise PreflightError("environment files must be regular owner-only files")
    if image_lock.is_symlink() or not image_lock.is_file() or image_lock.stat().st_mode & 0o077:
        raise PreflightError("image lock must be a regular owner-only file")
    if recovery_only:
        if platform.system() != "Linux" or platform.machine().lower() not in {"aarch64", "arm64"}:
            raise PreflightError("recovery host must remain native Linux ARM64")
        memory_mib = _parse_meminfo()
        free_gib = shutil.disk_usage("/").free / GIB
    else:
        memory_mib, free_gib = _validate_host(required_free_gib)
    check_local_lock(image_lock.resolve(), docker_prefix)
    image_lock_data = json.loads(image_lock.read_text(encoding="utf-8"))
    locked_images = {row["image"] for row in image_lock_data.get("images", [])}
    locked_images.update(
        row["localImage"] for row in image_lock_data.get("externalImages", {}).values()
    )
    config = _compose_config(docker_prefix, compose_files, env_files)
    cgroup_budget_mib, _cgroup_cpus = _validate_compose(
        config,
        mode,
        max_memory_mib,
        max_cpus,
        locked_images,
        ingress_exposure,
    )
    if recovery_only:
        memory = {
            "status": "RECOVERY_CONFIGURATION_ONLY",
            "memAvailableMiB": _parse_memavailable(),
            "minimumMemAvailableMiB": minimum_mem_available_mib,
            "additionalPhaseMemoryMiB": 0,
            "runtimeRssMeasurementRequired": False,
        }
    else:
        memory = _memory_headroom(
            mode=mode,
            config=config,
            available_mib=_parse_memavailable(),
            cgroup_budget_mib=cgroup_budget_mib,
            minimum_available_mib=minimum_mem_available_mib,
            connector_auth_phase=connector_auth_phase,
        )
    _validate_internal_https_topology(config)
    profile_mode = "cutover" if mode == "connector-auth-readonly" else mode
    _validate_profile_stage(config, profile_mode)
    _validate_provider_listener_addresses(config)
    _validate_teams_delivery_spool(config)
    notification_inputs = _validate_secrets_and_configs(
        config,
        secrets_dir.resolve(),
        config_dir.resolve(),
        mode,
        connector_auth_phase=connector_auth_phase,
        notification_anchor_file=notification_anchor_file,
        notification_provenance_file=notification_provenance_file,
        candidate_runpod_key_file=candidate_runpod_key_file,
    )
    automation_workflows_present = _validate_automation_workflow_configuration(
        config,
        config_dir=config_dir.resolve(),
        secrets_dir=secrets_dir.resolve(),
    )
    automation_providers_present = bool(_automation_provider_ids(config))
    navigator_environment = config.get("services", {}).get("navigator", {}).get("environment", {})
    execution_backend = navigator_environment.get("CYRENE_HARNESS_EXECUTION_BACKEND", "dsh")
    if execution_backend == "codebuddy-acp":
        _validate_codebuddy_acp_boundary(
            config,
            secrets_dir=secrets_dir.resolve(),
            config_dir=config_dir.resolve(),
            connector_auth_phase=connector_auth_phase,
        )
    if navigator_environment.get("CYRENE_RESPONSIBILITY_TARGETS_FILE"):
        if mode == "bootstrap":
            _validate_optional_workflow_definition_disabled(config_dir.resolve())
        else:
            _validate_github_workflow_allowlist(config, config_dir.resolve())
    _validate_identity_scope(config, config_dir.resolve() / "teams-actor-subjects.json")
    if (
        navigator_environment.get("CYRENE_HARNESS_MODEL_SOURCE") == "antigravity"
        or execution_backend == "codebuddy-acp"
    ):
        if navigator_environment.get("CYRENE_HARNESS_MODEL_SOURCE") == "antigravity":
            _validate_model_home_mount(config)
        _validate_proactive_navigator_container(config)
    _validate_stage_acceptance_receipts(
        mode=mode,
        config=config,
        github_hf_evidence=github_hf_evidence,
        azure_gcp_ledger=azure_gcp_ledger,
        runpod_ledger=runpod_ledger,
        azure_gcp_api_evidence=azure_gcp_api_evidence,
        runpod_api_evidence=runpod_api_evidence,
    )
    connector_auth_evidence_sha256: str | None = None
    connector_auth_expectations: dict[str, Any] | None = None
    if mode == "connector-auth-readonly":
        connector_auth_expectations = _connector_auth_scope_expectations(
            config,
            huggingface_seed_file=huggingface_seed_file,
            runpod_key_file=candidate_runpod_key_file,
        )
        operator = config.get("services", {}).get("directory-operator", {}).get("environment", {})
        expected_owner = operator.get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID")
        if not isinstance(expected_owner, str):
            raise PreflightError("Directory operator subject is unavailable for connector evidence")
        connector_auth_evidence_sha256 = _validate_connector_auth_readonly_evidence(
            connector_auth_evidence,
            expected_task_id=connector_auth_task_id,
            expected_owner=expected_owner,
            expected_scope_digests=connector_auth_expectations["expectedProviderScopeDigests"],
        )
    if mode != "bootstrap":
        _validate_codebuddy_mount(config, secrets_dir.resolve())
    owned_staging_image_id = _validate_host_ports(
        config,
        mode=mode,
        docker_prefix=docker_prefix,
        image_lock=image_lock_data,
        allow_owned_staging_ingress=allow_owned_staging_ingress,
        ingress_exposure=ingress_exposure,
    )
    host_fronted_ingress_readiness: dict[str, Any] | None = None
    if ingress_exposure == "host-fronted-staging":
        ingress_service = config.get("services", {}).get("teams-public-ingress", {})
        ingress_environment = (
            ingress_service.get("environment", {}) if isinstance(ingress_service, dict) else {}
        )
        expected_hostname = (
            ingress_environment.get("TEAMS_FQDN") if isinstance(ingress_environment, dict) else None
        )
        if not isinstance(expected_hostname, str) or not expected_hostname:
            raise PreflightError("host-fronted staging requires the candidate Teams FQDN")
        try:
            import host_fronted_canary

            host_fronted_ingress_readiness = (
                host_fronted_canary.run_existing_host_fronted_readiness(
                    expected_hostname=expected_hostname,
                    ingress_image_id=owned_staging_image_id,
                    caddy_binary=host_caddy_binary,
                    openssl_binary=openssl_binary,
                )
            )
        except (OSError, host_fronted_canary.CanaryGateError, subprocess.SubprocessError) as error:
            raise PreflightError("existing host-fronted ingress readiness check failed") from error
    runtime_rss = _validate_cutover_runtime(config, docker_prefix, mode)
    if len(compose_files) == 0 or len(env_files) == 0:
        raise PreflightError("compose and environment files are required")
    result = {
        **memory,
        "mode": mode,
        "deploymentReady": mode not in {"bootstrap", "connector-auth-readonly"}
        and not recovery_only,
        "status": (
            "RECOVERY_CONFIGURATION_ONLY"
            if recovery_only
            else "BOOTSTRAP_ONLY_NOT_FULL_READY"
            if mode == "bootstrap"
            else "CREDENTIAL_READ_ONLY_RUNTIME_READY"
            if mode == "connector-auth-readonly"
            else memory["status"]
        ),
        "hostArchitecture": platform.machine().lower(),
        "hostMemoryMiB": memory_mib,
        "rootFreeGiB": round(free_gib, 2),
        "composeCgroupMemoryLimitMiB": round(cgroup_budget_mib),
        "composeCgroupCpuLimit": round(_cgroup_cpus, 2),
        "runtimeRssByServiceMiB": runtime_rss,
        "computerRuntimeStorage": _computer_runtime_storage_report(config),
        "serviceCount": len(config["services"]),
        "imageLock": str(image_lock.resolve()),
    }
    if automation_workflows_present:
        result["automationWorkflowConfiguration"] = "PREPARE_ONLY_VALIDATED"
        result["automationWorkflowsEnabled"] = (
            navigator_environment.get("CYRENE_WORKFLOWS_ENABLED") == "true"
        )
        result["automationSourceAuthorization"] = "NOT_CHECKED_BY_LOCAL_PREFLIGHT"
    if automation_providers_present:
        result["automationProviderConfiguration"] = "PREPARE_ONLY_VALIDATED"
        result["automationProviderOAuthAndSourceAccess"] = "NOT_RUN"
    if mode == "connector-auth-readonly":
        result.update(
            {
                "connectorAuthPhase": connector_auth_phase,
                "credentialReadOnlyRuntimeReady": True,
                "fullCloudWriteDeploymentReady": False,
                "lifecycleAcceptance": "NOT_RUN",
                "connectorAuthEvidenceSha256": connector_auth_evidence_sha256,
                "providerIdentityEvidenceStatus": "OPERATOR_ATTESTED",
                "providerIdentityEvidenceClass": "OPERATOR_RECORDED_REAL_PROBE",
                "providerIdentityEvidenceCryptographicallyVerified": False,
                **(connector_auth_expectations or {}),
                **notification_inputs,
            }
        )
    if host_fronted_ingress_readiness is not None:
        result["hostFrontedIngressReadiness"] = host_fronted_ingress_readiness
    if owned_staging_image_id is not None:
        result["ownedStagingIngressImageId"] = owned_staging_image_id
    return result


def main() -> int:
    """Run a read-only deployment acceptance gate; never start Compose services."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=(
            "bootstrap",
            "staging",
            "cutover",
            "stage1",
            "stage2",
            "stage3",
            "connector-auth-readonly",
        ),
        required=True,
    )
    parser.add_argument("--compose-file", type=Path, action="append", required=True)
    parser.add_argument("--env-file", type=Path, action="append", required=True)
    parser.add_argument("--image-lock", type=Path, required=True)
    parser.add_argument("--secrets-dir", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--github-hf-evidence", type=Path)
    parser.add_argument("--azure-gcp-ledger", type=Path)
    parser.add_argument("--azure-gcp-api-evidence", type=Path)
    parser.add_argument("--runpod-ledger", type=Path)
    parser.add_argument("--runpod-api-evidence", type=Path)
    parser.add_argument("--connector-auth-phase", choices=sorted(CONNECTOR_AUTH_READONLY_PHASES))
    parser.add_argument("--connector-auth-evidence", type=Path)
    parser.add_argument("--connector-auth-task-id")
    parser.add_argument("--huggingface-seed-file", type=Path)
    parser.add_argument("--notification-anchor-file", type=Path)
    parser.add_argument("--notification-provenance-file", type=Path)
    parser.add_argument("--candidate-runpod-key-file", type=Path)
    parser.add_argument(
        "--ingress-exposure",
        choices=("public-cutover", "host-fronted-staging"),
        default="public-cutover",
    )
    parser.add_argument("--host-caddy-binary", default="/usr/local/bin/caddy")
    parser.add_argument("--openssl-binary", default="openssl")
    parser.add_argument("--docker-prefix", nargs="+", default=["sudo", "-n", "docker"])
    parser.add_argument("--max-memory-mib", type=int, default=3584)
    parser.add_argument("--max-cpus", type=float, default=1.95)
    parser.add_argument("--required-free-gib", type=float, default=2.0)
    parser.add_argument("--minimum-mem-available-mib", type=int, default=1024)
    parser.add_argument(
        "--allow-owned-staging-ingress",
        action="store_true",
        help="allow only a verified project-owned 127.0.0.1:18443 listener in stage1/staging mode",
    )
    arguments = parser.parse_args()
    try:
        result = run_preflight(
            mode=arguments.mode,
            compose_files=[path.resolve() for path in arguments.compose_file],
            env_files=[path.resolve() for path in arguments.env_file],
            image_lock=arguments.image_lock.resolve(),
            secrets_dir=arguments.secrets_dir.resolve(),
            config_dir=arguments.config_dir.resolve(),
            github_hf_evidence=arguments.github_hf_evidence,
            azure_gcp_ledger=arguments.azure_gcp_ledger,
            runpod_ledger=arguments.runpod_ledger,
            azure_gcp_api_evidence=arguments.azure_gcp_api_evidence,
            runpod_api_evidence=arguments.runpod_api_evidence,
            docker_prefix=arguments.docker_prefix,
            max_memory_mib=arguments.max_memory_mib,
            max_cpus=arguments.max_cpus,
            required_free_gib=arguments.required_free_gib,
            minimum_mem_available_mib=arguments.minimum_mem_available_mib,
            allow_owned_staging_ingress=arguments.allow_owned_staging_ingress,
            connector_auth_phase=arguments.connector_auth_phase,
            connector_auth_evidence=arguments.connector_auth_evidence,
            connector_auth_task_id=arguments.connector_auth_task_id,
            huggingface_seed_file=arguments.huggingface_seed_file,
            notification_anchor_file=arguments.notification_anchor_file,
            notification_provenance_file=arguments.notification_provenance_file,
            candidate_runpod_key_file=arguments.candidate_runpod_key_file,
            ingress_exposure=arguments.ingress_exposure,
            host_caddy_binary=arguments.host_caddy_binary,
            openssl_binary=arguments.openssl_binary,
        )
    except (OSError, PreflightError, BuildError, subprocess.SubprocessError) as error:
        print(f"Cloud deployment preflight blocked: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
