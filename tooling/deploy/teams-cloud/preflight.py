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
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from build_arm64 import BuildError, check_local_lock

GIB = 1024**3
MIB = 1024**2
# A nominal 4 GiB VM reports less MemTotal after firmware/kernel reservations.
# 中文：4 GiB 规格按 guest 可见下限识别；实际可用内存仍由独立余量检查决定。
MIN_GUEST_MEMORY_MIB = 3584
SECRET_SIZE_MINIMUMS = {
    "authority-signing-seed": 32,
    "authority-tls-key": 1,
    "exchange-authority-client-key": 1,
    "navigator-exchange-hmac-key": 32,
    "teams-navigator-hmac-key": 32,
    "account-center-encryption-key": 32,
    "account-center-exchange-hmac-key": 32,
    "cloud-management-exchange-hmac-key": 32,
    "github-issues-webhook-secret": 32,
}
RAW32_SECRET_NAMES = {"authority-signing-seed", "navigator-exchange-hmac-key"}
HEX32_SECRET_NAMES = {
    "account-center-exchange-hmac-key",
    "account-center-github-provider-hmac-key",
    "account-center-hf-provider-hmac-key",
    "cloud-management-exchange-hmac-key",
    "exchange-hf-auth-context-hmac-key",
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
    "account-center-github-app-private-key",
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
}
PLACEHOLDER_RE = re.compile(
    r"(replace[_ -]?with|example\.invalid|changeme|placeholder|<[^>]+>)", re.IGNORECASE
)
SECRET_NAME_RE = re.compile(r"(secret|token|password|private.?key|api.?key)", re.IGNORECASE)


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
    value = path.read_bytes()
    if expected_name in RAW32_SECRET_NAMES and len(value) != 32:
        raise PreflightError(f"raw HMAC/signing key must contain exactly 32 bytes: {expected_name}")
    if expected_name in HEX32_SECRET_NAMES and not re.fullmatch(rb"[0-9a-fA-F]{64}", value):
        raise PreflightError(
            f"HMAC key must contain exactly 64 hex characters without a newline: {expected_name}"
        )
    if expected_name == "teams-navigator-hmac-key" and not re.fullmatch(rb"[0-9a-fA-F]{64}", value):
        raise PreflightError("Teams-Navigator key must be ASCII hex64 without a trailing newline")
    if expected_name == "account-center-encryption-key":
        try:
            decoded = base64.urlsafe_b64decode(value)
        except (ValueError, binascii.Error) as error:
            raise PreflightError(
                "Account Center encryption key must be a Fernet URL-safe base64 key"
            ) from error
        if len(decoded) != 32 or base64.urlsafe_b64encode(decoded) != value:
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


def _validate_principal_configuration(path: Path, services: dict[str, Any]) -> None:
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
    if (
        not isinstance(principals, list)
        or len(principals) != 1
        or not isinstance(principals[0], dict)
    ):
        raise PreflightError(
            "production Navigator must use exactly one bootstrapped owner principal"
        )
    principal = principals[0]
    if (
        principal.get("actor_id") != expected_actor
        or principal.get("workspace_ids") != [expected_workspace]
        or principal.get("organization_id") != expected_organization
        or principal.get("token_env") != "NAVIGATOR_SESSION_TOKEN"
        or principal.get("can_takeover") is not True
    ):
        raise PreflightError(
            "Navigator must use the scoped owner as its trusted durable Work writer principal"
        )
    if document.get("product_directory") != [] or document.get("product_service_credentials") != []:
        raise PreflightError("unconfigured Product proxy credentials must remain absent")


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


CONNECTOR_OPERATION_POLICIES = {
    "github.repository.list": ("github.repository.list", "profile", False),
    "github.repository.get": ("github.repository.get", "github_repository", False),
    "github.branches.list": ("github.branches.list", "github_repository", False),
    "github.pull_requests.list": ("github.pull_requests.list", "github_repository", False),
    "github.pull_request.get": ("github.pull_request.get", "github_pull_request", False),
    "github.actions.runs.list": ("github.actions.runs.list", "github_repository", False),
    "github.ci.status": ("github.ci.status", "github_commit", False),
    "github.issue.get": ("github.issue.get", "github_issue", False),
    "github.issue.comment.create": ("github.issue.comment.create", "github_issue", True),
    "github.issue.state.update": ("github.issue.state.update", "github_issue", True),
    "github.vendor.cli.execute": ("github_vendor_cli", "profile", True),
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


def _validate_connector_bindings(
    path: Path, expected_providers: set[str], *, allow_empty: bool = False
) -> None:
    """Require exact Exchange-issued stage bindings and canonical operation maps."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreflightError("Navigator connector bindings are invalid JSON") from error
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
    seen: set[tuple[str, str]] = set()
    supported_providers = {"github", "huggingface", "azure", "gcp", "runpod"}
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
        key = (binding["providerId"], profile)
        if key in seen:
            raise PreflightError("Navigator provider profile bindings must be unique")
        seen.add(key)
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
                or (provider in {"azure", "gcp", "runpod"} and operation.startswith("cloud."))
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
    """Bind an Exchange-exported tool catalog to the exact host connection map."""

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
    if (
        not isinstance(catalog, dict)
        or set(catalog) != {"schemaVersion", "items"}
        or catalog.get("schemaVersion") != 1
        or isinstance(catalog.get("schemaVersion"), bool)
        or not isinstance(catalog.get("items"), list)
        or not isinstance(bindings, dict)
        or not isinstance(bindings.get("bindings"), list)
        or PLACEHOLDER_RE.search(json.dumps(catalog, sort_keys=True))
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


def _expected_connector_providers(config: dict[str, Any], mode: str) -> set[str]:
    """Derive the only provider bindings that may be mounted for a deployment stage."""

    if mode in {"stage1", "staging"}:
        return {"github", "huggingface"}
    if mode == "stage2":
        return {"github", "huggingface", "azure", "gcp"}
    if mode == "stage3":
        return {"github", "huggingface", "azure", "gcp", "runpod"}
    providers = {"github", "huggingface"}
    services = config.get("services", {})
    cloud_service = services.get("cloud-management-phase2")
    if isinstance(cloud_service, dict):
        providers.update({"azure", "gcp"})
    if isinstance(cloud_service, dict) and "runpod" in cloud_service.get("profiles", []):
        providers.add("runpod")
    return providers


def _validate_profile_stage(compose: dict[str, Any], mode: str) -> None:
    """Verify that optional provider profiles are attached only in their stage overlay."""

    services = compose.get("services", {})
    _validate_exchange_profile_stage(compose, mode)
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


def _expected_exchange_providers(compose: dict[str, Any], mode: str) -> set[str]:
    """Return the exact Exchange host-profile providers enabled in one phase."""

    if mode in {"stage1", "staging"}:
        return {"hf"}
    if mode == "stage2":
        return {"hf", "azure", "gcp"}
    if mode == "stage3":
        return {"hf", "azure", "gcp", "runpod"}
    providers = {"hf"}
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
        or platform_workload.get("connectionRef") != "grpcs://authority:7443"
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
    cloud = document.get("authorizationCloud")
    profiles = cloud.get("profiles") if isinstance(cloud, dict) else None
    if not isinstance(profiles, dict):
        raise PreflightError("Exchange requires the phase-scoped Hugging Face profile map")
    actual_providers = {
        profile.get("provider") for profile in profiles.values() if isinstance(profile, dict)
    }
    expected_providers = _expected_exchange_providers(compose, mode)
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


def _validate_account_center_config(service: dict[str, Any]) -> None:
    """Ensure GitHub and fresh HF OAuth credentials stay in host-managed files."""

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
    provider_file_refs = config.get("provider_secret_file_refs")
    file_refs = config.get("hostSecretFileRefs")
    if provider_file_refs != ["huggingface/connector-token"] or file_refs != {
        "huggingface/connector-token": "/run/secrets/huggingface-oauth.json"
    }:
        raise PreflightError(
            "Account Center must resolve the Hugging Face OAuth seed from its dedicated secret file"
        )
    if config.get("provider_secret_refs", []) or any(
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
    if not isinstance(file_refs, dict) or set(file_refs.values()) != {
        "/run/secrets/huggingface-oauth.json"
    }:
        raise PreflightError(
            "Account Center Hugging Face file reference must use its private mount"
        )


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


def _validate_caddyfile(service: dict[str, Any], mode: str) -> None:
    """Require the exact callback-only Caddy route and stage-appropriate TLS file."""

    matches = [
        row
        for row in service.get("volumes", [])
        if isinstance(row, dict) and row.get("target") == "/etc/caddy/Caddyfile"
    ]
    if len(matches) != 1 or matches[0].get("type") != "bind":
        raise PreflightError("public ingress must bind exactly one repository Caddyfile")
    path = Path(matches[0].get("source", ""))
    expected_name = (
        "Caddyfile.staging"
        if mode in {"bootstrap", "staging", "stage1", "stage2", "stage3"}
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
    if mode == "cutover":
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
            _validate_caddyfile(service, mode)
            if mode in {"bootstrap", "staging", "stage1", "stage2", "stage3"}:
                if any(host_ip not in {"127.0.0.1", "::1"} for _, host_ip in targets.values()):
                    raise PreflightError("staging ingress must bind loopback only")
                if any(published == "443" for published, _ in targets.values()):
                    raise PreflightError(
                        "staging ingress must use a loopback high port rather than host 443"
                    )
            if mode == "cutover":
                if {published for published, _ in targets.values()} != {"443"}:
                    raise PreflightError("cutover ingress must bind standard HTTPS port 443 only")
                if any(host_ip in {"127.0.0.1", "::1"} for _, host_ip in targets.values()):
                    raise PreflightError("cutover ingress must bind the VM public interface")
        profiles = set(service.get("profiles", []))
        if profiles & {"directory-bootstrap", "directory-operator"}:
            continue
        if name == "cloud-management-phase2":
            active_phase = "runpod" if mode == "stage3" else "cloud-phase2"
            if mode not in {"stage2", "stage3", "cutover"} or active_phase not in profiles:
                continue
        memory_value = service.get("mem_limit")
        if isinstance(memory_value, int) and memory_value > 0:
            amount_mib = memory_value / MIB
        elif isinstance(memory_value, str) and re.fullmatch(r"[0-9]+[kKmMgG]", memory_value):
            amount = int(memory_value[:-1])
            suffix = memory_value[-1].lower()
            amount_mib = amount * {"k": 1 / 1024, "m": 1, "g": 1024}[suffix]
        elif isinstance(memory_value, str) and memory_value.isdecimal() and int(memory_value) > 0:
            amount_mib = int(memory_value) / MIB
        else:
            raise PreflightError(f"Compose service {name} lacks a finite memory limit")
        memory += amount_mib
        try:
            cpus += float(service.get("cpus"))
        except (TypeError, ValueError) as error:
            raise PreflightError(f"Compose service {name} lacks a finite CPU limit") from error
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
    """Render Compose locally without starting or changing any service."""

    argv = [*docker_prefix, "compose"]
    for env_file in env_files:
        argv.extend(["--env-file", str(env_file.resolve())])
    for compose_file in compose_files:
        argv.extend(["--file", str(compose_file.resolve())])
    for profile in ("public-ingress", "cloud-phase2", "runpod"):
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
    config: dict[str, Any], secrets_dir: Path, config_dir: Path, mode: str
) -> None:
    """Check every file-backed secret/config path while keeping its contents private."""

    for directory, name in ((secrets_dir, "secret"), (config_dir, "configuration")):
        if not _private_artifact(directory) or directory.is_symlink() or not directory.is_dir():
            raise PreflightError(f"private {name} directory must remain under umbrella .artifacts")
        metadata = directory.stat()
        if metadata.st_uid != 10001 or metadata.st_mode & 0o077:
            raise PreflightError(
                f"private {name} directory must be owner-only and owned by UID 10001"
            )
    resolved_secrets = config.get("secrets", {})
    phase_secrets = {
        "cloud-management-exchange-hmac-key": {"stage2", "stage3", "cutover"},
        "gcp-wif-credential-config": {"stage2", "stage3", "cutover"},
        "runpod-api-key": {"stage3", "cutover"},
    }
    for secret_name, secret in resolved_secrets.items():
        if secret_name in phase_secrets and mode not in phase_secrets[secret_name]:
            continue
        source = secret.get("file") if isinstance(secret, dict) else None
        if not isinstance(source, str):
            raise PreflightError(f"secret {secret_name} must resolve to one host file")
        path = Path(source)
        if secret_name == "account-center-huggingface-oauth-source":
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
        if path.parent.resolve() != secrets_dir.resolve():
            raise PreflightError(
                f"secret {secret_name} is outside the configured private secret directory"
            )
        _secret_file(path, path.name)
    _validate_directory_database_urls(secrets_dir)
    _validate_internal_service_certificates(secrets_dir)
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
                _expected_connector_providers(config, mode),
                allow_empty=mode == "bootstrap",
            )
        elif path.name == "connector-tool-catalog.json":
            _validate_connector_tool_catalog(
                path,
                config_dir / "connector-bindings.json",
                _expected_connector_providers(config, mode),
                allow_empty=mode == "bootstrap",
            )
        elif path.name == "navigator-principal-config.json":
            _validate_principal_configuration(path, config.get("services", {}))
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
        _validate_account_center_config(account_center)
    cloud_service = config.get("services", {}).get("cloud-management-phase2")
    if isinstance(cloud_service, dict) and mode in {"stage2", "stage3", "cutover"}:
        _validate_cloud_acceptance_mount(cloud_service)
    oauth_mounts = [
        service_name
        for service_name, service in config.get("services", {}).items()
        for row in service.get("secrets", [])
        if isinstance(row, dict) and row.get("source") == "account-center-huggingface-oauth-source"
    ]
    if oauth_mounts != ["account-center"]:
        raise PreflightError(
            "Hugging Face OAuth bootstrap secret must mount only into Account Center"
        )
    teams_provider = config.get("services", {}).get("teams-provider")
    navigator = config.get("services", {}).get("navigator")
    if isinstance(teams_provider, dict) and isinstance(navigator, dict):
        _validate_teams_connector_config(teams_provider, navigator)


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
    """Require one tenant, one team, one owner, and the explicit CodeBuddy model source."""

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
    if navigator.get("CYRENE_HARNESS_MODEL_SOURCE") != "codebuddy" or navigator.get(
        "CYRENE_HARNESS_MODEL"
    ) not in {"glm-5.3-flash", "deepseek-v4.1-flash"}:
        raise PreflightError(
            "production Navigator must use the explicit CodeBuddy model source and an approved model"
        )
    if navigator.get("CYRENE_EXECUTOR_MAX_CONCURRENT_TASKS") != "1":
        raise PreflightError("the 2-vCPU host profile permits only one active Navigator task")


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


def _validate_host_ports(config: dict[str, Any]) -> None:
    """Refuse an ingress binding that already has a listener on this VM."""

    service = config["services"].get("teams-public-ingress", {})
    ports = service.get("ports", [])
    if not ports:
        return
    listeners = _run(["ss", "-H", "-ltn"])
    for row in ports:
        published = str(row.get("published", ""))
        if published and re.search(rf":{re.escape(published)}(?:\s|$)", listeners):
            raise PreflightError(
                f"host port {published} is already occupied; preserve the current ingress and plan a cutover"
            )


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
) -> dict[str, Any]:
    """Perform all read-only gates before the operator starts or cuts over services."""

    if mode not in {"bootstrap", "staging", "cutover", "stage1", "stage2", "stage3"}:
        raise PreflightError("mode must be bootstrap, staging, cutover, stage1, stage2, or stage3")
    if not _private_artifact(image_lock) or any(not _private_artifact(path) for path in env_files):
        raise PreflightError(
            "image locks and environment files must be stored under umbrella .artifacts"
        )
    for env_file in env_files:
        if env_file.is_symlink() or not env_file.is_file() or env_file.stat().st_mode & 0o077:
            raise PreflightError("environment files must be regular owner-only files")
    if image_lock.is_symlink() or not image_lock.is_file() or image_lock.stat().st_mode & 0o077:
        raise PreflightError("image lock must be a regular owner-only file")
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
    )
    memory = _memory_headroom(
        mode=mode,
        config=config,
        available_mib=_parse_memavailable(),
        cgroup_budget_mib=cgroup_budget_mib,
        minimum_available_mib=minimum_mem_available_mib,
    )
    _validate_internal_https_topology(config)
    _validate_profile_stage(config, mode)
    _validate_secrets_and_configs(config, secrets_dir.resolve(), config_dir.resolve(), mode)
    _validate_identity_scope(config, config_dir.resolve() / "teams-actor-subjects.json")
    _validate_stage_acceptance_receipts(
        mode=mode,
        config=config,
        github_hf_evidence=github_hf_evidence,
        azure_gcp_ledger=azure_gcp_ledger,
        runpod_ledger=runpod_ledger,
        azure_gcp_api_evidence=azure_gcp_api_evidence,
        runpod_api_evidence=runpod_api_evidence,
    )
    if mode != "bootstrap":
        _validate_codebuddy_mount(config, secrets_dir.resolve())
    _validate_host_ports(config)
    runtime_rss = _validate_cutover_runtime(config, docker_prefix, mode)
    if len(compose_files) == 0 or len(env_files) == 0:
        raise PreflightError("compose and environment files are required")
    return {
        **memory,
        "mode": mode,
        "deploymentReady": mode != "bootstrap",
        "status": "BOOTSTRAP_ONLY_NOT_FULL_READY" if mode == "bootstrap" else memory["status"],
        "hostArchitecture": platform.machine().lower(),
        "hostMemoryMiB": memory_mib,
        "rootFreeGiB": round(free_gib, 2),
        "composeCgroupMemoryLimitMiB": round(cgroup_budget_mib),
        "composeCgroupCpuLimit": round(_cgroup_cpus, 2),
        "runtimeRssByServiceMiB": runtime_rss,
        "serviceCount": len(config["services"]),
        "imageLock": str(image_lock.resolve()),
    }


def main() -> int:
    """Run a read-only deployment acceptance gate; never start Compose services."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("bootstrap", "staging", "cutover", "stage1", "stage2", "stage3"),
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
    parser.add_argument("--docker-prefix", nargs="+", default=["sudo", "-n", "docker"])
    parser.add_argument("--max-memory-mib", type=int, default=3072)
    parser.add_argument("--max-cpus", type=float, default=1.8)
    parser.add_argument("--required-free-gib", type=float, default=2.0)
    parser.add_argument("--minimum-mem-available-mib", type=int, default=1024)
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
        )
    except (OSError, PreflightError, BuildError, subprocess.SubprocessError) as error:
        print(f"Cloud deployment preflight blocked: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
