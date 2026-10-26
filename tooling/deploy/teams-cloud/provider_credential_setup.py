"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 provider_credential_setup.py                                          │
│  Module: tooling.deploy.teams_cloud.provider_credential_setup             │
│  Role: Provide a loopback-only owner page for HF/Runpod candidates.        │
│                                                                          │
│  模块职责：提供仅 loopback 可访问的 HF 与 Runpod 凭据配置页面。          │
│  · 服务端处理凭据 · 只读校验成功后原子写入 UID 10001 候选配置           │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import hmac
import html
import http.client
import json
import os
import re
import secrets
import ssl
import stat
import tempfile
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from preflight import (
    PreflightError,
    _private_artifact,
    _secret_file,
    _unique_json_object,
    _valid_huggingface_repository_scope,
    _validate_huggingface_oauth_source,
)

HF_HOST = "huggingface.co"
HF_CLIENT_ID = "26be6b09-91c5-47da-9861-d2d2bb7a7e36"
HF_BASE_SCOPES = ("openid", "profile", "read-repos")
HF_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
HF_SECRET_REF = "huggingface/connector-token"
HF_PROFILE_ALIAS = "hf-primary"
HF_MAX_RESPONSE_BYTES = 2_000_000
HF_MAX_TOKEN_BYTES = 16_384
HF_MAX_TOKEN_TTL = 366 * 24 * 60 * 60
MAX_HF_REPOSITORIES = 100
MAX_RUNPOD_RESPONSE_BYTES = 1_000_000
MAX_FORM_BYTES = 256_000
MAX_KEY_BYTES = 16_384
MAX_SEARCH_CHARS = 100
MAX_PAGE_SIZE = 100
RUNPOD_HOST = "api.runpod.io"
RUNPOD_PODS_PATH = "/v2/pods?limit=1"
SESSION_COOKIE = "cyrene_provider_setup"
SESSION_TTL_SECONDS = 1800
OWNER_UID = 10001
LOCK_FILE_NAME = ".provider-credential-setup.lock"
HF_REPO_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
HF_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
HF_FEATURED_OWNER = "Qwen"
HF_ALLOWED_SCOPES = frozenset({"openid", "profile", "read-repos", "gated-repos", "write-repos"})
HF_OAUTH_ERRORS = frozenset(
    {
        "access_denied",
        "authorization_pending",
        "expired_token",
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_scope",
        "slow_down",
        "unauthorized_client",
    }
)

HF_REPO_TYPE_PATH = {"model": "/api/models", "dataset": "/api/datasets", "space": "/api/spaces"}
HF_REPO_TYPES = frozenset(HF_REPO_TYPE_PATH)


class SetupError(ValueError):
    """A sanitized setup failure that never includes provider response bodies."""


OAuthTransport = Callable[[str, Mapping[str, str]], tuple[int, dict[str, Any]]]
HubTransport = Callable[[str, Mapping[str, str]], tuple[int, Any]]
RunpodTransport = Callable[[str], tuple[int, Any]]


@dataclass
class DeviceFlow:
    """Keep device credentials and pending authorization state in process memory."""

    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_at: int
    requested_scopes: tuple[str, ...]
    last_poll_at: float = 0.0


@dataclass
class SetupSession:
    """Hold one browser session's CSRF token and short-lived OAuth state."""

    csrf_token: str
    created_at: float = field(default_factory=time.monotonic)
    device_flow: DeviceFlow | None = None
    seed: dict[str, Any] | None = None
    identity: dict[str, Any] | None = None
    repositories: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    runpod_candidate: dict[str, Any] | None = None


def _safe_token(value: Any, *, maximum: int = HF_MAX_TOKEN_BYTES) -> bool:
    """Accept bounded printable bearer text without exposing it in errors."""

    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and all(33 <= ord(character) < 127 for character in value)
    )


def _oauth_post(path: str, fields: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
    """Send form data only to Hugging Face's device and token endpoints."""

    if path not in {"/oauth/device", "/oauth/token"}:
        raise SetupError("oauth_endpoint_invalid")
    connection = http.client.HTTPSConnection(
        HF_HOST, timeout=15, context=ssl.create_default_context()
    )
    try:
        body = urllib.parse.urlencode(fields).encode("ascii")
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Cyrene-Provider-Setup/1.0",
            },
        )
        response = connection.getresponse()
        raw = response.read(HF_MAX_RESPONSE_BYTES + 1)
        if len(raw) > HF_MAX_RESPONSE_BYTES:
            raise SetupError("oauth_response_too_large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SetupError("oauth_response_invalid") from None
        if not isinstance(payload, dict):
            raise SetupError("oauth_response_invalid")
        return response.status, payload
    except (OSError, ssl.SSLError, TimeoutError):
        raise SetupError("oauth_connection_failed") from None
    finally:
        connection.close()


def _hub_get(path: str, headers: Mapping[str, str]) -> tuple[int, Any]:
    """GET a fixed Hugging Face identity or repository-list endpoint."""

    if not path.startswith(("/api/whoami-v2", "/api/models?", "/api/datasets?", "/api/spaces?")):
        raise SetupError("huggingface_endpoint_invalid")
    connection = http.client.HTTPSConnection(
        HF_HOST, timeout=20, context=ssl.create_default_context()
    )
    try:
        connection.request("GET", path, headers=dict(headers))
        response = connection.getresponse()
        raw = response.read(HF_MAX_RESPONSE_BYTES + 1)
        if len(raw) > HF_MAX_RESPONSE_BYTES:
            raise SetupError("huggingface_response_too_large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SetupError("huggingface_response_invalid") from None
        return response.status, payload
    except (OSError, ssl.SSLError, TimeoutError):
        raise SetupError("huggingface_connection_failed") from None
    finally:
        connection.close()


def _runpod_get(api_key: str) -> tuple[int, Any]:
    """Probe a Runpod key with the non-mutating REST v2 Pods list endpoint."""

    if not _safe_token(api_key, maximum=MAX_KEY_BYTES):
        raise SetupError("runpod_key_invalid")
    connection = http.client.HTTPSConnection(
        RUNPOD_HOST, timeout=15, context=ssl.create_default_context()
    )
    try:
        connection.request(
            "GET",
            RUNPOD_PODS_PATH,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Cyrene-Provider-Setup/1.0",
            },
        )
        response = connection.getresponse()
        raw = response.read(MAX_RUNPOD_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RUNPOD_RESPONSE_BYTES:
            raise SetupError("runpod_response_too_large")
        if response.status != HTTPStatus.OK:
            return response.status, None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SetupError("runpod_response_invalid") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("pods"), list):
            raise SetupError("runpod_response_invalid")
        return response.status, payload
    except (OSError, ssl.SSLError, TimeoutError):
        raise SetupError("runpod_connection_failed") from None
    finally:
        connection.close()


def _private_directory(path: Path, *, create: bool = False) -> Path:
    """Require a UID 10001 owner-only directory outside all source checkouts."""

    if path.is_symlink():
        raise SetupError("private_directory_invalid")
    if create:
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        try:
            for directory in reversed(missing):
                directory.mkdir(mode=0o700)
        except OSError:
            raise SetupError("private_directory_unavailable") from None
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat()
    except OSError:
        raise SetupError("private_directory_unavailable") from None
    if (
        not path.is_dir()
        or not _private_artifact(resolved)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != OWNER_UID
        or hasattr(os, "geteuid")
        and os.geteuid() != OWNER_UID
    ):
        raise SetupError("private_directory_permissions_invalid")
    return resolved


def _atomic_private_write(path: Path, payload: bytes, *, mode: int) -> None:
    """Atomically create or replace one UID 10001 owner-only private file."""

    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SetupError("private_output_invalid")
    if path.exists():
        metadata = path.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != OWNER_UID
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise SetupError("private_output_permissions_invalid")
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, filename = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(filename)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(temporary, OWNER_UID, -1)
        elif hasattr(os, "geteuid") and os.geteuid() != OWNER_UID:
            raise SetupError("private_output_owner_invalid")
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except SetupError:
        raise
    except OSError:
        raise SetupError("private_output_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_private_runtime_env(path: Path) -> tuple[list[str], dict[str, str]]:
    """Read a private Compose env file without sourcing or evaluating it."""

    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        recorded = path.lstat()
        if (
            path.is_symlink()
            or absolute != resolved
            or not _private_artifact(resolved)
            or not stat.S_ISREG(recorded.st_mode)
            or recorded.st_nlink != 1
            or recorded.st_uid != OWNER_UID
            or stat.S_IMODE(recorded.st_mode) & 0o077
            or recorded.st_size > 1_000_000
        ):
            raise SetupError("runtime_env_permissions_invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != OWNER_UID
                or not metadata.st_mode & 0o400
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_size > 1_000_000
                or (metadata.st_dev, metadata.st_ino) != (recorded.st_dev, recorded.st_ino)
            ):
                raise SetupError("runtime_env_permissions_invalid")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise SetupError("runtime_env_permissions_invalid")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except SetupError:
        raise
    except OSError:
        raise SetupError("runtime_env_unavailable") from None
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SetupError("runtime_env_encoding_invalid") from None
    if "\x00" in content:
        raise SetupError("runtime_env_encoding_invalid")

    values: dict[str, str] = {}
    lines = content.splitlines(keepends=True)
    required = {
        "CYRENE_HF_OAUTH_BOOTSTRAP_FILE",
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON",
        "CYRENE_HUGGINGFACE_CONFIGURATION_JSON",
    }
    for name in (
        "CYRENE_HF_OAUTH_BOOTSTRAP_FILE",
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON",
        "CYRENE_HUGGINGFACE_CONFIGURATION_JSON",
        "CYRENE_RUNPOD_API_KEY_FILE",
    ):
        found: list[str] = []
        for line in lines:
            body = line.rstrip("\r\n")
            if body.startswith(f"{name}="):
                found.append(body[len(name) + 1 :])
            elif body.strip().startswith((f"export {name}", f"{name} ", f"{name}\t")):
                raise SetupError("runtime_env_assignment_unsupported")
        if len(found) > 1 or (name in required and len(found) != 1):
            raise SetupError("runtime_env_assignment_invalid")
        if found:
            values[name] = found[0]
    return lines, values


def _canonical_hf_repositories(repositories: Any) -> list[dict[str, Any]]:
    """Normalize the closed HF repository scope shared by seed and provider config."""

    if not _valid_huggingface_repository_scope(repositories):
        raise SetupError("huggingface_repository_scope_invalid")
    normalized = [
        {
            "repo_type": row["repo_type"],
            "repo_id": row["repo_id"],
            "permissions": sorted(row["permissions"]),
        }
        for row in repositories
    ]
    return sorted(normalized, key=lambda row: (row["repo_type"], row["repo_id"]))


def _json_object_without_duplicates(value: str, error_code: str) -> dict[str, Any]:
    """Parse one compact env-file JSON value and reject duplicate object keys."""

    try:
        parsed = json.loads(value, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, ValueError):
        raise SetupError(error_code) from None
    if not isinstance(parsed, dict):
        raise SetupError(error_code)
    return parsed


def _hf_candidate_env(
    lines: list[str],
    values: Mapping[str, str],
    repositories: list[dict[str, Any]],
    seed_path: Path,
) -> str:
    """Render only the HF seed path and its two matching repository scopes."""

    exchange = _json_object_without_duplicates(
        values["CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"],
        "exchange_configuration_invalid",
    )
    authorization = exchange.get("authorizationCloud")
    profiles = authorization.get("profiles") if isinstance(authorization, dict) else None
    profile = profiles.get(HF_PROFILE_ALIAS) if isinstance(profiles, dict) else None
    hf_profiles = (
        [
            name
            for name, row in profiles.items()
            if isinstance(row, dict) and row.get("provider") == "hf"
        ]
        if isinstance(profiles, dict)
        else []
    )
    if (
        not isinstance(profile, dict)
        or profile.get("provider") != "hf"
        or hf_profiles != [HF_PROFILE_ALIAS]
    ):
        raise SetupError("huggingface_exchange_profile_unavailable")
    direct_plugin = profile.get("directPlugin")
    if (
        not isinstance(direct_plugin, dict)
        or direct_plugin.get("connectionRef") != "grpcs://huggingface:7443"
        or direct_plugin.get("transport") != "mtls"
    ):
        raise SetupError("huggingface_exchange_profile_invalid")
    account_scopes = profile.setdefault("accountScopes", {})
    if not isinstance(account_scopes, dict):
        raise SetupError("huggingface_exchange_profile_invalid")
    account_scopes["repositories"] = [
        {
            "repoType": row["repo_type"],
            "repoId": row["repo_id"],
            "permissions": row["permissions"],
        }
        for row in repositories
    ]

    provider = _json_object_without_duplicates(
        values["CYRENE_HUGGINGFACE_CONFIGURATION_JSON"],
        "huggingface_provider_configuration_invalid",
    )
    provider["repositories"] = repositories
    provider["read_only"] = not any("write" in row["permissions"] for row in repositories)
    replacements = {
        "CYRENE_HF_OAUTH_BOOTSTRAP_FILE": str(seed_path),
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": json.dumps(
            exchange, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "CYRENE_HUGGINGFACE_CONFIGURATION_JSON": json.dumps(
            provider, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    }
    if any(
        any(character in replacements[name] for character in "\r\n\x00$")
        or re.search(r"\s#", replacements[name]) is not None
        for name in replacements
    ):
        raise SetupError("runtime_env_value_invalid")
    if any(character in str(seed_path) for character in " \t\r\n\x00$#'\"\\"):
        raise SetupError("runtime_env_path_unsupported")
    output: list[str] = []
    replaced: set[str] = set()
    for line in lines:
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        body = line[: -len(ending)] if ending else line
        match = re.match(r"^(CYRENE_[A-Z0-9_]+)=(.*)$", body)
        if match and match.group(1) in replacements:
            name = match.group(1)
            output.append(f"{name}={replacements[name]}{ending}")
            replaced.add(name)
        else:
            output.append(line)
    if replaced != set(replacements):
        raise SetupError("runtime_env_assignment_invalid")
    return "".join(output)


def _runpod_candidate_env(lines: list[str], key_path: Path) -> str:
    """Render the isolated Runpod key path without changing other secret sources."""

    replacement = str(key_path)
    if any(character in replacement for character in " \t\r\n\x00$#'\"\\"):
        raise SetupError("runtime_env_path_unsupported")
    output: list[str] = []
    replaced = False
    for line in lines:
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        body = line[: -len(ending)] if ending else line
        if body.startswith("CYRENE_RUNPOD_API_KEY_FILE="):
            output.append(f"CYRENE_RUNPOD_API_KEY_FILE={replacement}{ending}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        if output and not output[-1].endswith(("\n", "\r")):
            output.append("\n")
        output.append(f"CYRENE_RUNPOD_API_KEY_FILE={replacement}\n")
    return "".join(output)


def _write_hf_candidate_bundle(
    artifacts_dir: Path,
    active_env_file: Path,
    seed: Mapping[str, Any],
) -> tuple[str, Path, Path, str]:
    """Atomically publish an immutable owner-only env/seed candidate directory."""

    lines, values = _read_private_runtime_env(active_env_file)
    repositories = _canonical_hf_repositories(seed["target_scope"]["repositories"])
    candidate_id = uuid.uuid4().hex
    parent = _private_directory(
        artifacts_dir / "onboarding" / "private" / "provider-candidates", create=True
    )
    staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=parent))
    os.chmod(staging, 0o700)
    if os.geteuid() == 0:
        os.chown(staging, OWNER_UID, -1)
    elif os.geteuid() != OWNER_UID:
        raise SetupError("private_output_owner_invalid")
    final_directory = parent / candidate_id
    candidate_seed = staging / "huggingface-oauth.json"
    candidate_env = staging / "runtime.env"
    try:
        rendered_env = _hf_candidate_env(
            lines, values, repositories, final_directory / candidate_seed.name
        )
        output_seed = dict(seed)
        output_seed["target_scope"] = {"repositories": repositories}
        _atomic_private_write(
            candidate_seed,
            (json.dumps(output_seed, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
                "utf-8"
            ),
            mode=0o400,
        )
        _atomic_private_write(candidate_env, rendered_env.encode("utf-8"), mode=0o600)
        try:
            _validate_huggingface_oauth_source(candidate_seed)
        except PreflightError:
            raise SetupError("huggingface_seed_validation_failed") from None
        if final_directory.exists() or final_directory.is_symlink():
            raise SetupError("candidate_generation_collision")
        os.rename(staging, final_directory)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except SetupError:
        raise
    except OSError:
        raise SetupError("candidate_bundle_write_failed") from None
    finally:
        if staging.exists():
            for child in (candidate_env, candidate_seed):
                child.unlink(missing_ok=True)
            staging.rmdir()

    candidate_env = final_directory / "runtime.env"
    candidate_seed = final_directory / "huggingface-oauth.json"
    if not candidate_env.is_file() or not candidate_seed.is_file():
        raise SetupError("candidate_bundle_publish_failed")
    for path, mode in ((candidate_env, 0o600), (candidate_seed, 0o400)):
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != OWNER_UID
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise SetupError("candidate_bundle_permissions_invalid")
    env_hash = hashlib.sha256(candidate_env.read_bytes()).hexdigest()
    return candidate_id, candidate_env, candidate_seed, env_hash


def _write_runpod_candidate_bundle(
    artifacts_dir: Path,
    active_env_file: Path,
    api_key: str,
) -> tuple[str, Path, Path, str, str]:
    """Publish a validated Runpod key and matching env without touching active secrets."""

    lines, _values = _read_private_runtime_env(active_env_file)
    candidate_id = uuid.uuid4().hex
    parent = _private_directory(
        artifacts_dir / "onboarding" / "private" / "provider-candidates", create=True
    )
    staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=parent))
    os.chmod(staging, 0o700)
    if os.geteuid() == 0:
        os.chown(staging, OWNER_UID, -1)
    elif os.geteuid() != OWNER_UID:
        raise SetupError("private_output_owner_invalid")
    final_directory = parent / candidate_id
    candidate_env = staging / "runtime.env"
    candidate_key = staging / "runpod-api-key"
    published = False
    try:
        rendered_env = _runpod_candidate_env(lines, final_directory / candidate_key.name)
        _atomic_private_write(candidate_env, rendered_env.encode("utf-8"), mode=0o600)
        _atomic_private_write(candidate_key, api_key.encode("ascii"), mode=0o400)
        try:
            _secret_file(candidate_key, "runpod-api-key")
        except PreflightError:
            raise SetupError("runpod_candidate_validation_failed") from None
        if final_directory.exists() or final_directory.is_symlink():
            raise SetupError("candidate_generation_collision")
        os.rename(staging, final_directory)
        published = True
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except SetupError:
        raise
    except OSError:
        raise SetupError(
            "candidate_bundle_durability_unknown" if published else "candidate_bundle_write_failed"
        ) from None
    finally:
        if staging.exists():
            candidate_env.unlink(missing_ok=True)
            candidate_key.unlink(missing_ok=True)
            staging.rmdir()

    candidate_env = final_directory / "runtime.env"
    candidate_key = final_directory / "runpod-api-key"
    for path, mode in ((candidate_env, 0o600), (candidate_key, 0o400)):
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != OWNER_UID
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise SetupError("candidate_bundle_permissions_invalid")
    return (
        candidate_id,
        candidate_env,
        candidate_key,
        hashlib.sha256(candidate_env.read_bytes()).hexdigest(),
        hashlib.sha256(candidate_key.read_bytes()).hexdigest(),
    )


def _validate_seed(seed: Any) -> dict[str, Any]:
    """Validate an existing owner-only HF seed without exposing its fields."""

    required = {
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
    if (
        not isinstance(seed, dict)
        or set(seed) != required
        or seed.get("schema_version") != 1
        or seed.get("provider") != "huggingface"
        or seed.get("auth_mode") != "device_oauth"
        or seed.get("credential_secret_ref") != HF_SECRET_REF
        or seed.get("client_id") != HF_CLIENT_ID
        or not _safe_token(seed.get("access_token"))
        or not _safe_token(seed.get("refresh_token"))
        or not isinstance(seed.get("oauth_scopes"), list)
        or any(not isinstance(scope, str) for scope in seed["oauth_scopes"])
        or len(seed["oauth_scopes"]) != len(set(seed["oauth_scopes"]))
        or not set(seed["oauth_scopes"]).issubset(HF_ALLOWED_SCOPES)
        or not isinstance(seed.get("target_scope"), dict)
        or set(seed["target_scope"]) != {"repositories"}
        or not isinstance(seed["target_scope"].get("repositories"), list)
        or not _valid_huggingface_repository_scope(seed["target_scope"]["repositories"])
    ):
        raise SetupError("huggingface_seed_invalid")
    expires = seed.get("access_token_expires_at")
    refresh_expires = seed.get("refresh_token_expires_at")
    if (
        not isinstance(expires, int)
        or isinstance(expires, bool)
        or expires <= 0
        or refresh_expires is not None
        and (
            not isinstance(refresh_expires, int)
            or isinstance(refresh_expires, bool)
            or refresh_expires <= 0
        )
    ):
        raise SetupError("huggingface_seed_invalid")
    return dict(seed)


def _read_seed_file(path: Path) -> dict[str, Any]:
    """Read a bounded owner-only seed file without returning data to the browser."""

    if (
        path.is_symlink()
        or path.parent.is_symlink()
        or not path.is_file()
        or not _private_artifact(path)
    ):
        raise SetupError("huggingface_seed_unavailable")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise SetupError("huggingface_seed_unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != OWNER_UID
            or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
            or metadata.st_size > 1_000_000
        ):
            raise SetupError("huggingface_seed_permissions_invalid")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            raw = stream.read(1_000_001)
        if len(raw) > 1_000_000:
            raise SetupError("huggingface_seed_permissions_invalid")
        seed = json.loads(raw.decode("utf-8"))
    except SetupError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SetupError("huggingface_seed_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _validate_seed(seed)


def _actual_oauth_scopes(payload: Mapping[str, Any], requested: tuple[str, ...]) -> tuple[str, ...]:
    """Require the granted OAuth scope set to match the explicit request."""

    value = payload.get("scope")
    if value is None:
        return tuple(sorted(requested))
    if not isinstance(value, str) or len(value) > 4096:
        raise SetupError("oauth_scope_response_invalid")
    scopes = value.split()
    if len(scopes) != len(set(scopes)) or set(scopes) != set(requested):
        raise SetupError("oauth_scope_mismatch")
    return tuple(sorted(scopes))


def _validate_device_response(payload: Mapping[str, Any]) -> tuple[str, str, str, int, int]:
    """Validate HF device-code fields and a fixed official verification URI."""

    device_code = payload.get("device_code")
    user_code = payload.get("user_code")
    verification_uri = payload.get("verification_uri")
    interval = payload.get("interval", 5)
    expires_in = payload.get("expires_in", 900)
    if (
        not _safe_token(device_code, maximum=2048)
        or not isinstance(user_code, str)
        or re.fullmatch(r"[A-Za-z0-9-]{4,32}", user_code) is None
        or not isinstance(verification_uri, str)
        or isinstance(interval, bool)
        or not isinstance(interval, int)
        or not 1 <= interval <= 60
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or not 1 <= expires_in <= 3600
    ):
        raise SetupError("oauth_device_response_invalid")
    parsed = urllib.parse.urlsplit(verification_uri)
    try:
        port = parsed.port
    except ValueError:
        raise SetupError("oauth_verification_uri_invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {HF_HOST, "hf.co"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise SetupError("oauth_verification_uri_invalid")
    return device_code, user_code, verification_uri, interval, expires_in


def _seed_from_token_response(
    payload: Mapping[str, Any], requested_scopes: tuple[str, ...]
) -> dict[str, Any]:
    """Build the existing Account Center seed DTO from a fresh OAuth grant."""

    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    refresh_expires_in = payload.get("refresh_token_expires_in", payload.get("refresh_expires_in"))
    token_type = payload.get("token_type")
    if (
        not _safe_token(access_token)
        or not _safe_token(refresh_token)
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or not 1 <= expires_in <= HF_MAX_TOKEN_TTL
        or refresh_expires_in is not None
        and (
            isinstance(refresh_expires_in, bool)
            or not isinstance(refresh_expires_in, int)
            or not 1 <= refresh_expires_in <= HF_MAX_TOKEN_TTL
        )
        or token_type is not None
        and (not isinstance(token_type, str) or token_type.casefold() != "bearer")
    ):
        raise SetupError("oauth_token_response_invalid")
    scopes = _actual_oauth_scopes(payload, requested_scopes)
    now = int(time.time())
    return {
        "schema_version": 1,
        "provider": "huggingface",
        "auth_mode": "device_oauth",
        "client_id": HF_CLIENT_ID,
        "oauth_scopes": list(scopes),
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_token_expires_at": now + expires_in,
        "refresh_token_expires_at": (
            now + refresh_expires_in if isinstance(refresh_expires_in, int) else None
        ),
        "target_scope": {"repositories": []},
        "credential_secret_ref": HF_SECRET_REF,
    }


class HuggingFaceClient:
    """Make fixed-origin, read-only Hub identity and repository-list calls."""

    def __init__(self, transport: HubTransport = _hub_get) -> None:
        self._transport = transport

    def whoami(self, token: str) -> dict[str, Any]:
        """Return only the verified username and organization slugs."""

        status, payload = self._transport(
            "/api/whoami-v2", {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        )
        if status != HTTPStatus.OK or not isinstance(payload, dict):
            raise SetupError("huggingface_identity_unavailable")
        username = payload.get("name") or payload.get("username")
        if not isinstance(username, str) or not HF_OWNER_RE.fullmatch(username):
            raise SetupError("huggingface_identity_invalid")
        organizations = payload.get("orgs", payload.get("organizations", []))
        if not isinstance(organizations, list):
            organizations = []
        org_names: list[str] = []
        for row in organizations:
            name = row.get("name") if isinstance(row, dict) else row
            if isinstance(name, str) and HF_OWNER_RE.fullmatch(name) and name not in org_names:
                org_names.append(name)
        return {"username": username, "organizations": sorted(org_names)}

    def repositories(
        self, token: str, repo_type: str, owner: str, search: str
    ) -> list[dict[str, Any]]:
        """Search a verified owner or public Hub namespace and return safe metadata."""

        path = HF_REPO_TYPE_PATH.get(repo_type)
        if path is None or (owner != "*" and not HF_OWNER_RE.fullmatch(owner)):
            raise SetupError("huggingface_repository_query_invalid")
        if (
            not isinstance(search, str)
            or len(search) > MAX_SEARCH_CHARS
            or any(ord(character) < 32 for character in search)
        ):
            raise SetupError("huggingface_search_invalid")
        if owner == "*" and len(search) < 2:
            raise SetupError("public_search_requires_query")
        query_values = {
            "search": search,
            "limit": str(MAX_PAGE_SIZE),
            "expand": "private,gated",
        }
        if owner != "*":
            query_values["author"] = owner
        query = urllib.parse.urlencode(query_values)
        status, payload = self._transport(
            f"{path}?{query}", {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        )
        if status != HTTPStatus.OK or not isinstance(payload, list):
            raise SetupError("huggingface_repository_list_unavailable")
        result: list[dict[str, Any]] = []
        for row in payload[:MAX_PAGE_SIZE]:
            repo_id = row.get("id") if isinstance(row, dict) else None
            if not isinstance(repo_id, str) or HF_REPO_ID_RE.fullmatch(repo_id) is None:
                continue
            result.append(
                {
                    "repoType": repo_type,
                    "repoId": repo_id,
                    "private": row.get("private") is True,
                    "gated": row.get("gated") is True
                    or isinstance(row.get("gated"), str)
                    and row.get("gated") in {"auto", "manual", "true"},
                }
            )
        return result


class CredentialSetupService:
    """Coordinate private files, provider probes, and one-time browser sessions."""

    def __init__(
        self,
        artifacts_dir: Path,
        secrets_dir: Path,
        *,
        oauth_transport: OAuthTransport = _oauth_post,
        hub_client: HuggingFaceClient | None = None,
        runpod_probe: RunpodTransport = _runpod_get,
        imported_hf_seed: Path | None = None,
        active_env_file: Path | None = None,
    ) -> None:
        self.artifacts_dir = artifacts_dir.resolve()
        self.secrets_dir = secrets_dir.resolve()
        self.active_env_file = Path(os.path.abspath(active_env_file)) if active_env_file else None
        self.oauth_transport = oauth_transport
        self.hub = hub_client or HuggingFaceClient()
        self.runpod_probe = runpod_probe
        self._lock = threading.RLock()
        self._sessions: dict[str, SetupSession] = {}
        _private_directory(self.artifacts_dir)
        _private_directory(self.secrets_dir)
        _private_directory(self.artifacts_dir / "onboarding", create=True)
        _private_directory(self.artifacts_dir / "onboarding" / "private", create=True)
        self.imported_hf_seed = _read_seed_file(imported_hf_seed) if imported_hf_seed else None

    @contextlib.contextmanager
    def _exclusive_write_lock(self) -> Iterator[None]:
        """Serialize local credential replacements through one owner-only file lock."""

        lock_path = self.artifacts_dir / "onboarding" / "private" / LOCK_FILE_NAME
        if lock_path.is_symlink():
            raise SetupError("setup_lock_invalid")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            metadata = os.fstat(descriptor)
            recorded = lock_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != OWNER_UID
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (metadata.st_dev, metadata.st_ino) != (recorded.st_dev, recorded.st_ino)
            ):
                os.close(descriptor)
                raise SetupError("setup_lock_invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                raise SetupError("setup_operation_in_progress") from None
        except SetupError:
            raise
        except OSError:
            raise SetupError("setup_lock_unavailable") from None
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def create_session(self) -> tuple[str, SetupSession]:
        """Create an unpredictable browser session and CSRF token."""

        session_id = secrets.token_urlsafe(32)
        session = SetupSession(csrf_token=secrets.token_urlsafe(32))
        with self._lock:
            self._sessions[session_id] = session
        return session_id, session

    def get_session(self, session_id: str | None) -> SetupSession:
        """Resolve one unexpired browser session without accepting caller paths."""

        if not isinstance(session_id, str):
            raise SetupError("session_required")
        with self._lock:
            self._expire_sessions_locked()
            session = self._sessions.get(session_id)
        if session is None:
            raise SetupError("session_expired")
        return session

    def _expire_sessions_locked(self) -> None:
        cutoff = time.monotonic() - SESSION_TTL_SECONDS
        expired = [key for key, value in self._sessions.items() if value.created_at < cutoff]
        for key in expired:
            self._sessions.pop(key, None)

    def status(self, session: SetupSession) -> dict[str, Any]:
        """Return non-secret setup status without making provider network calls."""

        seed = session.seed or self.imported_hf_seed
        hf_status: dict[str, Any] = {"state": "NOT_CONFIGURED"}
        if seed is not None:
            session.seed = seed
            hf_status = {
                "state": (
                    "REAUTHORIZATION_REQUIRED"
                    if seed["access_token_expires_at"] <= int(time.time())
                    else "READY_FOR_SELECTION"
                ),
                "expiresAt": seed["access_token_expires_at"],
                "scopes": seed["oauth_scopes"],
                "selectedRepositories": len(seed["target_scope"]["repositories"]),
            }
        elif session.device_flow is not None:
            hf_status = {
                "state": "AUTHORIZATION_PENDING"
                if session.device_flow.expires_at > int(time.time())
                else "DEVICE_CODE_EXPIRED"
            }
        runpod_status = session.runpod_candidate or {"state": "NO_CANDIDATE"}
        return {
            "huggingface": hf_status,
            "huggingfaceCandidateInput": "AVAILABLE"
            if self.active_env_file is not None
            else "RUNTIME_ENV_FILE_REQUIRED",
            "runpod": runpod_status,
        }

    def hf_identity(self, session: SetupSession) -> dict[str, Any]:
        """Resolve the account and namespaces after an explicit user action."""

        seed = session.seed or self.imported_hf_seed
        if seed is None or seed["access_token_expires_at"] <= int(time.time()):
            raise SetupError("huggingface_reauthorization_required")
        session.identity = self.hub.whoami(seed["access_token"])
        return session.identity

    def start_hf_device_flow(
        self,
        session: SetupSession,
        *,
        write_access: bool,
        include_gated: bool,
    ) -> dict[str, Any]:
        """Start official HF device OAuth without persisting device or token material."""

        requested = list(HF_BASE_SCOPES)
        if include_gated:
            requested.append("gated-repos")
        if write_access:
            requested.append("write-repos")
        status, payload = self.oauth_transport(
            "/oauth/device",
            {"client_id": HF_CLIENT_ID, "scope": " ".join(requested)},
        )
        if status != HTTPStatus.OK:
            code = payload.get("error")
            raise SetupError(
                code if isinstance(code, str) and code in HF_OAUTH_ERRORS else "oauth_device_failed"
            )
        device, user, verification_uri, interval, expires_in = _validate_device_response(payload)
        session.device_flow = DeviceFlow(
            device_code=device,
            user_code=user,
            verification_uri=verification_uri,
            interval=interval,
            expires_at=int(time.time()) + expires_in,
            requested_scopes=tuple(requested),
        )
        session.seed = None
        session.identity = None
        session.repositories.clear()
        return {
            "state": "AUTHORIZATION_PENDING",
            "verificationUri": verification_uri,
            "userCode": user,
            "expiresAt": session.device_flow.expires_at,
            "requestedScopes": requested,
        }

    def poll_hf_device_flow(self, session: SetupSession) -> dict[str, Any]:
        """Poll one official HF device grant and retain tokens only in memory."""

        flow = session.device_flow
        if flow is None or flow.expires_at <= int(time.time()):
            session.device_flow = None
            return {"state": "DEVICE_CODE_EXPIRED"}
        now = time.monotonic()
        if flow.last_poll_at and now - flow.last_poll_at < flow.interval:
            return {"state": "AUTHORIZATION_PENDING", "retryAfterSeconds": flow.interval}
        flow.last_poll_at = now
        try:
            status, payload = self.oauth_transport(
                "/oauth/token",
                {
                    "grant_type": HF_DEVICE_GRANT,
                    "device_code": flow.device_code,
                    "client_id": HF_CLIENT_ID,
                },
            )
        except SetupError as error:
            if str(error) == "oauth_connection_failed":
                return {"state": "AUTHORIZATION_PENDING", "retryAfterSeconds": flow.interval}
            raise
        if status == HTTPStatus.OK and "access_token" in payload:
            session.seed = _seed_from_token_response(payload, flow.requested_scopes)
            session.device_flow = None
            try:
                identity = self.hub.whoami(session.seed["access_token"])
            except SetupError:
                session.seed = None
                raise
            session.identity = identity
            return {
                "state": "READY_FOR_SELECTION",
                "username": identity["username"],
                "organizations": identity["organizations"],
                "expiresAt": session.seed["access_token_expires_at"],
                "scopes": session.seed["oauth_scopes"],
            }
        code = payload.get("error")
        if code == "authorization_pending":
            return {"state": "AUTHORIZATION_PENDING", "retryAfterSeconds": flow.interval}
        if code == "slow_down":
            flow.interval = min(60, flow.interval + 5)
            return {"state": "AUTHORIZATION_PENDING", "retryAfterSeconds": flow.interval}
        if code in {"expired_token", "access_denied"}:
            session.device_flow = None
            return {"state": "DEVICE_CODE_EXPIRED" if code == "expired_token" else "ACCESS_DENIED"}
        if status == 429 or status >= 500:
            return {"state": "AUTHORIZATION_PENDING", "retryAfterSeconds": flow.interval}
        safe_code = (
            code if isinstance(code, str) and code in HF_OAUTH_ERRORS else "oauth_token_failed"
        )
        session.device_flow = None
        raise SetupError(safe_code)

    def list_hf_repositories(
        self, session: SetupSession, *, repo_type: str, owner: str, search: str
    ) -> dict[str, Any]:
        """List bounded repository metadata under the verified HF account scope."""

        seed = session.seed or self.imported_hf_seed
        if seed is None or seed["access_token_expires_at"] <= int(time.time()):
            raise SetupError("huggingface_reauthorization_required")
        identity = session.identity
        if identity is None:
            raise SetupError("huggingface_namespaces_not_loaded")
        allowed_owners = {identity["username"], *identity["organizations"]}
        if owner != "*" and owner not in allowed_owners | {HF_FEATURED_OWNER}:
            raise SetupError("huggingface_namespace_unavailable")
        repositories = self.hub.repositories(seed["access_token"], repo_type, owner, search)
        for repository in repositories:
            session.repositories[(repo_type, repository["repoId"])] = repository
        return {"repositories": repositories}

    def save_hf_selection(self, session: SetupSession, selection: Any) -> dict[str, Any]:
        """Persist an HF OAuth seed only for repository IDs listed by this session."""

        seed = session.seed or self.imported_hf_seed
        if seed is None or seed["access_token_expires_at"] <= int(time.time()):
            raise SetupError("huggingface_reauthorization_required")
        identity = session.identity
        if identity is None:
            raise SetupError("huggingface_namespaces_not_loaded")
        allowed_owners = {identity["username"], *identity["organizations"]}
        if not isinstance(selection, list) or not 1 <= len(selection) <= MAX_HF_REPOSITORIES:
            raise SetupError("huggingface_selection_invalid")
        scopes = set(seed["oauth_scopes"])
        selected: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in selection:
            if not isinstance(row, dict) or set(row) != {"repoType", "repoId", "permissions"}:
                raise SetupError("huggingface_selection_invalid")
            repo_type = row.get("repoType")
            repo_id = row.get("repoId")
            permissions = row.get("permissions")
            if (
                not isinstance(repo_type, str)
                or repo_type not in HF_REPO_TYPES
                or not isinstance(repo_id, str)
                or HF_REPO_ID_RE.fullmatch(repo_id) is None
                or not isinstance(permissions, list)
                or not permissions
                or any(not isinstance(permission, str) for permission in permissions)
                or set(permissions) - {"read", "write"}
                or len(permissions) != len(set(permissions))
                or ("write" in permissions and "write-repos" not in scopes)
                or ("read" not in permissions and "write" not in permissions)
                or (repo_type, repo_id) in seen
                or (repo_type, repo_id) not in session.repositories
            ):
                raise SetupError("huggingface_selection_out_of_scope")
            seen.add((repo_type, repo_id))
            metadata = session.repositories[(repo_type, repo_id)]
            namespace = repo_id.split("/", maxsplit=1)[0]
            if metadata.get("private") and namespace not in allowed_owners:
                raise SetupError("huggingface_selection_out_of_scope")
            if metadata.get("gated") and "gated-repos" not in scopes:
                raise SetupError("huggingface_gated_scope_missing")
            selected.append(
                {
                    "repo_type": repo_type,
                    "repo_id": repo_id,
                    "permissions": sorted(permissions),
                }
            )
        output = dict(seed)
        output["target_scope"] = {"repositories": _canonical_hf_repositories(selected)}
        if self.active_env_file is None:
            raise SetupError("runtime_env_required_for_huggingface_candidate")
        with self._exclusive_write_lock():
            candidate_id, candidate_env, candidate_seed, env_hash = _write_hf_candidate_bundle(
                self.artifacts_dir, self.active_env_file, output
            )
        session.seed = output
        session.repositories.clear()
        return {
            "state": "SAVED_CANDIDATE_PROFILE_LINK_OR_ROTATION_REQUIRED",
            "username": identity["username"],
            "repositoryCount": len(selected),
            "accessMode": "read_write"
            if any("write" in row["permissions"] for row in selected)
            else "read_only",
            "expiresAt": output["access_token_expires_at"],
            "candidateId": candidate_id,
            "candidateEnvFile": str(candidate_env),
            "candidateHuggingFaceSeed": str(candidate_seed),
            "candidateEnvSha256": env_hash,
            "exchangeProfileAlias": HF_PROFILE_ALIAS,
            "activation": "FORMAL_STAGE_TRANSITION_REQUIRED",
        }

    def save_runpod_key(self, session: SetupSession, api_key: str) -> dict[str, Any]:
        """Validate a key with GET Pods, then write a private stage-transition candidate."""

        if not isinstance(api_key, str):
            raise SetupError("runpod_key_invalid")
        api_key = api_key.strip()
        if not _safe_token(api_key, maximum=MAX_KEY_BYTES):
            raise SetupError("runpod_key_invalid")
        if self.active_env_file is None:
            raise SetupError("runtime_env_required_for_runpod_candidate")
        with self._exclusive_write_lock():
            status, payload = self.runpod_probe(api_key)
            if status == HTTPStatus.UNAUTHORIZED:
                raise SetupError("runpod_authentication_failed")
            if status == HTTPStatus.FORBIDDEN:
                raise SetupError("runpod_pods_read_permission_denied")
            if status == HTTPStatus.TOO_MANY_REQUESTS:
                raise SetupError("runpod_rate_limited")
            if (
                status != HTTPStatus.OK
                or not isinstance(payload, dict)
                or not isinstance(payload.get("pods"), list)
            ):
                raise SetupError("runpod_validation_failed")
            (
                candidate_id,
                candidate_env,
                candidate_key,
                env_hash,
                key_hash,
            ) = _write_runpod_candidate_bundle(self.artifacts_dir, self.active_env_file, api_key)
        result = {
            "state": "SAVED_CANDIDATE_RELOAD_REQUIRED_READ_VALIDATED",
            "candidateId": candidate_id,
            "candidateEnvFile": str(candidate_env),
            "candidateRunpodKeyFile": str(candidate_key),
            "candidateEnvSha256": env_hash,
            "candidateRunpodKeySha256": key_hash,
            "activation": "FORMAL_STAGE_TRANSITION_REQUIRED",
        }
        session.runpod_candidate = {
            "state": result["state"],
            "candidateEnvFile": result["candidateEnvFile"],
            "candidateRunpodKeyFile": result["candidateRunpodKeyFile"],
            "activation": result["activation"],
        }
        return result


def _private_page() -> str:
    """Return the static setup page; dynamic provider data arrives as JSON text."""

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cyrene provider credential setup</title>
<style nonce="{nonce}">body{{font:16px system-ui;max-width:820px;margin:2rem auto;padding:0 1rem;color:#172033}}section{{border:1px solid #ccd3df;border-radius:12px;padding:1rem 1.25rem;margin:1.25rem 0}}label{{display:block;margin:.65rem 0}}input,select,button{{font:inherit;padding:.45rem}}input[type=search]{{width:min(100%,28rem)}}.row{{display:flex;gap:1rem;align-items:center;flex-wrap:wrap}}#repo-results label{{border-bottom:1px solid #ddd;padding:.4rem}}.status{{white-space:pre-wrap;color:#334155}}small{{color:#526070}}</style>
</head><body>
<h1>Cyrene provider authorization</h1>
<p>This page listens only on 127.0.0.1. Credentials are sent from this browser to the local helper and are never echoed or logged.</p>
<section><h2>Hugging Face</h2><p>Use the existing authorized device-OAuth seed when available. Choose only repositories you want Cyrene to access. Saving creates a private candidate runtime.env and OAuth seed together; it does not change the active deployment.</p>
<div id="hf-state" class="status">Loading authorization state…</div>
<button id="hf-load-owner" type="button">Load account and organization namespaces</button>
<div class="row"><label>Namespace<select id="hf-owner"><option value="*">Public Hub search</option></select></label><label>Repository type<select id="hf-type"><option value="model">Models</option><option value="dataset">Datasets</option><option value="space">Spaces</option></select></label></div>
<div class="row"><label>Search<input id="hf-search" type="search" maxlength="100"></label><button id="hf-search-button" type="button">Search repositories</button><button id="hf-qwen" type="button">Find the Qwen starter model</button></div>
<div id="repo-results"></div><button id="hf-save" type="button">Save selected repositories</button>
<p><small>Saving publishes a private env/seed candidate only. The Exchange profile must then be linked or rotated through its formal profile action; this page does not activate a provider connection.</small></p>
<details><summary>Authorize a new HF session</summary><p>Starts the official Hugging Face device flow. Tokens remain in this helper process and are saved only after repository selection.</p>
<label><input id="hf-write" type="checkbox"> Request write-repos access (the account token may be broader than the selected repository allowlist).</label>
<label><input id="hf-gated" type="checkbox"> Request access to gated public repositories.</label>
<button id="hf-start" type="button">Start Hugging Face login</button>
<div id="hf-device" class="status"></div></details></section>
<section><h2>Runpod</h2><p>Validation calls only <code>GET https://api.runpod.io/v2/pods?limit=1</code>. It proves Pod-list access, not write permissions. A successful key is saved in a versioned private candidate bundle; the active key is not changed.</p>
<form method="post" action="/runpod" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label>Runpod API key <input type="password" name="api_key" required maxlength="16384" autocomplete="new-password"></label>
<button type="submit">Validate and save</button></form><div id="runpod-state" class="status">No validation run.</div>
<p><small>Create a Restricted key and grant only the Pod operations required by your approved profile. A read-only list check cannot verify write access.</small></p></section>
<script nonce="{nonce}">
const csrf = {csrf_json};
async function api(path, body) {{
  const response = await fetch(path, {{method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {{}} : {{'Content-Type':'application/json','X-Cyrene-CSRF':csrf}},
    body: body === undefined ? undefined : JSON.stringify({{...body, csrf}}), credentials:'same-origin'}});
  const data = await response.json(); if (!response.ok) throw new Error(data.error || 'request_failed'); return data;
}}
function show(id, value) {{ document.getElementById(id).textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2); }}
let repositories = [];
async function refreshStatus() {{
  const data = await api('/status');
  show('hf-state', {{...data.huggingface, candidateInput:data.huggingfaceCandidateInput}});
  show('runpod-state', data.runpod.state);
}}
async function loadNamespaces() {{
  const data = await api('/hf/owners'); const owner = document.getElementById('hf-owner'); owner.replaceChildren();
  const publicOption = document.createElement('option'); publicOption.value='*'; publicOption.textContent='Public Hub search'; owner.append(publicOption);
  for (const name of [data.username, ...(data.organizations || [])]) {{
    const option = document.createElement('option'); option.value=name; option.textContent=name; owner.append(option);
  }}
  show('hf-state', {{state:'READY_FOR_SELECTION',username:data.username,organizations:data.organizations}});
}}
document.getElementById('hf-load-owner').addEventListener('click', async () => {{
  try {{ await loadNamespaces(); }} catch(error) {{ show('hf-state',error.message); }}
}});
document.getElementById('hf-search-button').addEventListener('click', async () => {{
  const params = new URLSearchParams({{type:document.getElementById('hf-type').value,
    owner:document.getElementById('hf-owner').value, search:document.getElementById('hf-search').value}});
  try {{ const data = await api('/hf/repositories?' + params.toString()); repositories = data.repositories;
    const list = document.getElementById('repo-results'); list.replaceChildren();
    for (const repo of repositories) {{
      const label = document.createElement('label');
      const check = document.createElement('input'); check.type='checkbox'; check.value=repo.repoId;
      const text = document.createElement('span'); text.textContent=`${{repo.repoId}}${{repo.private?' · private':''}}${{repo.gated?' · gated':''}}`;
      const permission = document.createElement('select'); permission.dataset.repo=repo.repoId;
      for (const value of ['read','read_write']) {{ const option=document.createElement('option'); option.value=value; option.textContent=value==='read'?'Read':'Read and write'; permission.append(option); }}
      label.append(check,text,permission); list.append(label);
    }}
  }} catch (error) {{ show('hf-state', error.message); }}
}});
document.getElementById('hf-qwen').addEventListener('click', async () => {{
  document.getElementById('hf-owner').value='*';
  document.getElementById('hf-type').value='model';
  document.getElementById('hf-search').value='Qwen2.5-0.5B-Instruct';
  document.getElementById('hf-search-button').click();
}});
document.getElementById('hf-save').addEventListener('click', async () => {{
  const selected = [...document.querySelectorAll('#repo-results input[type=checkbox]:checked')].map(check => {{
    const repo = repositories.find(row => row.repoId === check.value); const permission = document.querySelector(`select[data-repo="${{CSS.escape(check.value)}}"]`).value;
    return {{repoType:repo.repoType,repoId:repo.repoId,permissions:permission==='read_write'?['read','write']:['read']}};
  }});
  try {{ show('hf-state', await api('/hf/save', {{repositories:selected}})); }} catch (error) {{ show('hf-state', error.message); }}
}});
document.getElementById('hf-start').addEventListener('click', async () => {{
  try {{ const data=await api('/hf/start', {{writeAccess:document.getElementById('hf-write').checked,
    includeGated:document.getElementById('hf-gated').checked}});
    const device=document.getElementById('hf-device'); device.replaceChildren();
    const link=document.createElement('a'); link.href=data.verificationUri; link.target='_blank'; link.rel='noopener noreferrer'; link.textContent=data.verificationUri;
    const code=document.createElement('strong'); code.textContent=data.userCode;
    device.append('Open ',link,' and enter code ',code);
    const poll=setInterval(async()=>{{try{{const state=await api('/hf/poll',{{}});show('hf-state',state);if(state.state!=='AUTHORIZATION_PENDING'){{clearInterval(poll);await refreshStatus();if(state.state==='READY_FOR_SELECTION'){{await loadNamespaces();}}}}}}catch(error){{show('hf-state',error.message);clearInterval(poll);}}}},3000);
  }} catch(error) {{ show('hf-state', error.message); }}
}});
document.querySelector('form[action="/runpod"]').addEventListener('submit', async event => {{
  event.preventDefault(); const field=event.currentTarget.elements.api_key; const key=field.value; field.value='';
  try {{ show('runpod-state',await api('/runpod',{{api_key:key}})); }} catch(error) {{ show('runpod-state',error.message); }}
}});
refreshStatus().catch(error=>show('hf-state',error.message));
</script></body></html>"""


class ProviderSetupServer(ThreadingHTTPServer):
    """Bind one provider setup service to the IPv4 loopback interface only."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        service: CredentialSetupService,
        *,
        server_nonce: str | None = None,
    ) -> None:
        self.service = service
        self.server_nonce = server_nonce or secrets.token_urlsafe(24)
        super().__init__(address, ProviderSetupHandler)
        self.expected_host = f"127.0.0.1:{self.server_address[1]}"


class ProviderSetupHandler(BaseHTTPRequestHandler):
    """Validate loopback Host, exact Origin, session cookie, and CSRF on every write."""

    server: ProviderSetupServer

    def log_message(self, _format: str, *_args: Any) -> None:
        """Suppress request logs so browser fields and one-time codes cannot leak."""

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'none'; style-src 'nonce-{self.server.server_nonce}'; "
            f"script-src 'nonce-{self.server.server_nonce}'; form-action 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, value: Mapping[str, Any]) -> None:
        self._send(status, json.dumps(value, separators=(",", ":")).encode(), "application/json")

    def _host_valid(self) -> bool:
        return self.headers.get("Host") == self.server.expected_host

    def _origin_valid(self) -> bool:
        return self.headers.get("Origin") == f"http://{self.server.expected_host}"

    def _session(self, *, create: bool = False) -> tuple[str, SetupSession]:
        cookie = self.headers.get("Cookie", "")
        session_id = None
        for item in cookie.split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == SESSION_COOKIE:
                session_id = value
                break
        if create and session_id is None:
            session_id, session = self.server.service.create_session()
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL_SECONDS}",
            )
            return session_id, session
        return session_id or "", self.server.service.get_session(session_id)

    def _send_json_error(self, error: SetupError, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._json(status, {"error": str(error)})

    def do_GET(self) -> None:
        if not self._host_valid():
            self._json(HTTPStatus.FORBIDDEN, {"error": "host_not_allowed"})
            return
        if self.path == "/":
            session_id = self._cookie_session_id()
            if session_id:
                try:
                    session = self.server.service.get_session(session_id)
                except SetupError:
                    session_id = ""
            if not session_id:
                session_id, session = self.server.service.create_session()
                csrf = session.csrf_token
                page = (
                    _private_page()
                    .format(
                        nonce=self.server.server_nonce,
                        csrf=html.escape(csrf, quote=True),
                        csrf_json=json.dumps(csrf),
                    )
                    .encode("utf-8")
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    f"default-src 'none'; style-src 'nonce-{self.server.server_nonce}'; script-src 'nonce-{self.server.server_nonce}'; form-action 'self'; frame-ancestors 'none'",
                )
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL_SECONDS}",
                )
                self.end_headers()
                self.wfile.write(page)
                return
            page = (
                _private_page()
                .format(
                    nonce=self.server.server_nonce,
                    csrf=html.escape(session.csrf_token, quote=True),
                    csrf_json=json.dumps(session.csrf_token),
                )
                .encode("utf-8")
            )
            self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            return
        try:
            session = self.server.service.get_session(self._cookie_session_id())
            if self.path == "/status":
                self._json(HTTPStatus.OK, self.server.service.status(session))
                return
            if self.path == "/hf/owners":
                self._json(HTTPStatus.OK, self.server.service.hf_identity(session))
                return
            if self.path.startswith("/hf/repositories?"):
                try:
                    params = urllib.parse.parse_qs(
                        urllib.parse.urlsplit(self.path).query, strict_parsing=True
                    )
                except ValueError:
                    raise SetupError("huggingface_repository_query_invalid") from None
                if set(params) != {"type", "owner", "search"} or any(
                    len(values) != 1 for values in params.values()
                ):
                    raise SetupError("huggingface_repository_query_invalid")
                result = self.server.service.list_hf_repositories(
                    session,
                    repo_type=params["type"][0],
                    owner=params["owner"][0],
                    search=params["search"][0],
                )
                self._json(HTTPStatus.OK, result)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except SetupError as error:
            self._send_json_error(
                error,
                HTTPStatus.UNAUTHORIZED
                if str(error) in {"session_required", "session_expired"}
                else HTTPStatus.BAD_REQUEST,
            )

    def _cookie_session_id(self) -> str | None:
        for item in self.headers.get("Cookie", "").split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == SESSION_COOKIE:
                return value
        return None

    def _read_json_body(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise SetupError("content_type_invalid")
        length_value = self.headers.get("Content-Length")
        if not length_value or not length_value.isdigit() or int(length_value) > MAX_FORM_BYTES:
            raise SetupError("request_body_invalid")
        try:
            payload = json.loads(self.rfile.read(int(length_value)).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SetupError("request_body_invalid") from None
        if not isinstance(payload, dict):
            raise SetupError("request_body_invalid")
        return payload

    def _authorize_post(self, payload: Mapping[str, Any]) -> SetupSession:
        if not self._host_valid() or not self._origin_valid():
            raise SetupError("origin_not_allowed")
        session = self.server.service.get_session(self._cookie_session_id())
        token = payload.get("csrf")
        header_token = self.headers.get("X-Cyrene-CSRF")
        if (
            not isinstance(token, str)
            or not isinstance(header_token, str)
            or not hmac.compare_digest(token, session.csrf_token)
            or not hmac.compare_digest(header_token, session.csrf_token)
        ):
            raise SetupError("csrf_invalid")
        return session

    def do_POST(self) -> None:
        try:
            payload = self._read_json_body()
            session = self._authorize_post(payload)
            if self.path == "/hf/start":
                if set(payload) != {
                    "csrf",
                    "writeAccess",
                    "includeGated",
                } or any(
                    not isinstance(payload.get(name), bool)
                    for name in ("writeAccess", "includeGated")
                ):
                    raise SetupError("request_body_invalid")
                result = self.server.service.start_hf_device_flow(
                    session,
                    write_access=payload["writeAccess"],
                    include_gated=payload["includeGated"],
                )
                self._json(HTTPStatus.OK, result)
                return
            if self.path == "/hf/poll":
                if set(payload) != {"csrf"}:
                    raise SetupError("request_body_invalid")
                self._json(HTTPStatus.OK, self.server.service.poll_hf_device_flow(session))
                return
            if self.path == "/hf/save":
                if set(payload) != {"csrf", "repositories"}:
                    raise SetupError("request_body_invalid")
                self._json(
                    HTTPStatus.OK,
                    self.server.service.save_hf_selection(session, payload["repositories"]),
                )
                return
            if self.path == "/runpod":
                if set(payload) != {"csrf", "api_key"} or not isinstance(
                    payload.get("api_key"), str
                ):
                    raise SetupError("request_body_invalid")
                self._json(
                    HTTPStatus.OK,
                    self.server.service.save_runpod_key(session, payload["api_key"]),
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except SetupError as error:
            status = (
                HTTPStatus.FORBIDDEN
                if str(error) in {"origin_not_allowed", "csrf_invalid"}
                else HTTPStatus.BAD_REQUEST
            )
            self._send_json_error(error, status)

    def do_OPTIONS(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})


def main(argv: list[str] | None = None) -> int:
    """Start the explicit owner-only page; this command never initiates OAuth."""

    parser = argparse.ArgumentParser(
        description="Serve a loopback-only provider credential setup page; OAuth starts only after an explicit browser action."
    )
    parser.add_argument("--artifacts-dir", required=True, type=Path)
    parser.add_argument(
        "--secrets-dir",
        required=True,
        type=Path,
        help="Existing private secrets directory, validated read-only; candidate credentials are never written here.",
    )
    parser.add_argument(
        "--hf-seed",
        type=Path,
        help="Optional existing owner-only HF OAuth seed for repository selection.",
    )
    parser.add_argument(
        "--active-env-file",
        type=Path,
        help="Private runtime.env input used read-only to generate HF or Runpod activation candidates.",
    )
    parser.add_argument("--host", choices=["127.0.0.1"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        service = CredentialSetupService(
            args.artifacts_dir,
            args.secrets_dir,
            imported_hf_seed=args.hf_seed,
            active_env_file=args.active_env_file,
        )
        server = ProviderSetupServer((args.host, args.port), service)
    except SetupError as error:
        print(f"status={error}")
        return 1
    print(f"url=http://127.0.0.1:{server.server_address[1]}/")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
