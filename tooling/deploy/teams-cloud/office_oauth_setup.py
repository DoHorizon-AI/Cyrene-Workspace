#!/usr/bin/env python3
"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 office_oauth_setup.py                                                 │
│  Module: tooling.deploy.teams_cloud.office_oauth_setup                   │
│  Role: Provide a loopback-only Microsoft 365 OAuth and target picker.     │
│                                                                          │
│  模块职责：提供仅 loopback 可访问的 Microsoft 365 OAuth 与目标选择页。    │
│  · OAuth 凭据交给 Exchange/Account Center 管理 · 不接受浏览器身份/权限   │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import secrets
import stat
import subprocess
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 52191
CALLBACK_PATH = "/oauth/callback"
CALLBACK_REDIRECT_URI = f"http://localhost:{DEFAULT_PORT}{CALLBACK_PATH}"
SESSION_COOKIE = "cyrene_office_setup"
SESSION_TTL_SECONDS = 1800
MAX_SESSIONS = 16
MAX_BODY_BYTES = 64_000
MAX_CLI_OUTPUT_BYTES = 2_000_000
MAX_PRIVATE_CONFIG_BYTES = 16_384
CLI_TIMEOUT_SECONDS = 60
OWNER_UID = 10001
MAX_DISCOVERY_PAGES = 10
MAX_DISCOVERY_ITEMS = 500
EXCHANGE_CLI = "/app/Cyrene-Services/Cyrene-Exchange/product/.venv/bin/cyrene-exchange"
INTEGRATION_DATABASE = "/data/integrations.sqlite3"
PRIVATE_OPERATOR_CONFIG = "/run/config/office-operator.json"
ALLOWED_DISCOVERY_TYPES = frozenset(
    {
        "planner_plans",
        "todo_lists",
        "sharepoint_drives",
        "calendars",
        "freebusy_subjects",
    }
)
OFFICE_FEATURE_KEYS = frozenset(
    {
        "plannerEnabled",
        "todoEnabled",
        "calendarEnabled",
        "mailMetadataEnabled",
        "mailDraftsEnabled",
        "mailSendEnabled",
        "sharePointDriveMetadataEnabled",
    }
)
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_ID_RE = re.compile(r"^[^\x00-\x20\x7f]{1,512}$")
_SAFE_SCOPE_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")
_AUTHORIZATION_HOSTS = frozenset({"login.microsoftonline.com"})
_EXCHANGE_PROVIDER_ID = "microsoft_graph"
_MAX_OAUTH_STATE_BYTES = 512
_MAX_OAUTH_CODE_BYTES = 8192


class OfficeSetupError(ValueError):
    """A sanitized setup failure that never includes an OAuth URL or value."""


class OfficeSetupBridge(Protocol):
    """Invoke the fixed Exchange office-setup command with one JSON envelope."""

    def invoke(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Run one allowlisted Exchange operation and return its safe JSON object."""


@dataclass(frozen=True, slots=True)
class OperatorContext:
    """Keep private owner pins in the server process for exact OAuth identity checks."""

    issuer: str
    tenant_id: str
    subject: str
    organization_id: str
    workspace_id: str
    binding_ref: str
    profile_idempotency_key: str

    @property
    def external_subject(self) -> str:
        """Return the exact Account Center tenant and Entra object identity tuple."""

        return f"{self.tenant_id}:{self.subject}"


def _canonical_uuid(value: Any, error_code: str) -> str:
    """Accept a canonical lowercase UUID for private Directory and OAuth pins."""

    if not isinstance(value, str):
        raise OfficeSetupError(error_code)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise OfficeSetupError(error_code) from None
    canonical = str(parsed)
    if canonical != value:
        raise OfficeSetupError(error_code)
    return canonical


def _operator_context_from_document(document: Any) -> OperatorContext:
    """Validate the exact nonsecret owner config consumed by Exchange and the UI server."""

    if (
        not isinstance(document, dict)
        or set(document) != {"schemaVersion", "actor", "bindingRef", "profileIdempotencyKey"}
        or document.get("schemaVersion") != 1
        or not isinstance(document.get("actor"), dict)
    ):
        raise OfficeSetupError("office_operator_config_invalid")
    actor = document["actor"]
    if set(actor) != {"issuer", "subject", "organizationId", "workspaceId"}:
        raise OfficeSetupError("office_operator_config_invalid")
    issuer = actor["issuer"]
    if not isinstance(issuer, str) or len(issuer) > 128:
        raise OfficeSetupError("office_operator_config_invalid")
    try:
        parsed_issuer = urllib.parse.urlsplit(issuer)
        issuer_parts = parsed_issuer.path.strip("/").split("/")
    except ValueError:
        raise OfficeSetupError("office_operator_config_invalid") from None
    if (
        parsed_issuer.scheme != "https"
        or parsed_issuer.hostname != "login.microsoftonline.com"
        or parsed_issuer.query
        or parsed_issuer.fragment
        or parsed_issuer.username is not None
        or parsed_issuer.password is not None
        or len(issuer_parts) != 2
        or issuer_parts[1] != "v2.0"
    ):
        raise OfficeSetupError("office_operator_config_invalid")
    tenant_id = _canonical_uuid(issuer_parts[0], "office_operator_config_invalid")
    subject = _canonical_uuid(actor["subject"], "office_operator_config_invalid")
    organization_id = _canonical_uuid(actor["organizationId"], "office_operator_config_invalid")
    workspace_id = _canonical_uuid(actor["workspaceId"], "office_operator_config_invalid")
    binding_ref = document["bindingRef"]
    idempotency_key = document["profileIdempotencyKey"]
    if (
        not isinstance(binding_ref, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", binding_ref)
        or not isinstance(idempotency_key, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", idempotency_key)
    ):
        raise OfficeSetupError("office_operator_config_invalid")
    return OperatorContext(
        issuer=issuer,
        tenant_id=tenant_id,
        subject=subject,
        organization_id=organization_id,
        workspace_id=workspace_id,
        binding_ref=binding_ref,
        profile_idempotency_key=idempotency_key,
    )


def load_private_operator_config(path: str = PRIVATE_OPERATOR_CONFIG) -> OperatorContext:
    """Read the nonsecret actor pins from the owner-only Exchange configuration file."""

    if os.geteuid() != OWNER_UID:
        raise OfficeSetupError("office_operator_config_owner_invalid")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise OfficeSetupError("office_operator_config_unavailable") from None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != OWNER_UID
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_PRIVATE_CONFIG_BYTES
        ):
            raise OfficeSetupError("office_operator_config_permissions_invalid")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_PRIVATE_CONFIG_BYTES:
            chunk = os.read(fd, min(4096, MAX_PRIVATE_CONFIG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        if total > MAX_PRIVATE_CONFIG_BYTES:
            raise OfficeSetupError("office_operator_config_too_large")
    except OSError:
        raise OfficeSetupError("office_operator_config_unavailable") from None
    finally:
        os.close(fd)
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise OfficeSetupError("office_operator_config_invalid") from None
    return _operator_context_from_document(document)


CliRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class ExchangeCliBridge:
    """Run only the fixed Exchange office-setup CLI in the current container."""

    def __init__(
        self,
        *,
        runner: CliRunner = subprocess.run,
        executable: str = EXCHANGE_CLI,
        integration_database: str = INTEGRATION_DATABASE,
        private_config: str = PRIVATE_OPERATOR_CONFIG,
    ) -> None:
        self._runner = runner
        self._executable = executable
        self._integration_database = integration_database
        self._private_config = private_config

    def invoke(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Pass the operation payload over stdin; keep codes out of argv and logs."""

        if operation not in {"begin", "status", "discover", "select-targets", "complete"}:
            raise OfficeSetupError("office_operation_invalid")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_BODY_BYTES:
            raise OfficeSetupError("office_request_too_large")
        command = [
            self._executable,
            "integrations",
            "office-setup",
            operation,
            "--integration-database",
            self._integration_database,
            "--private-config",
            self._private_config,
        ]
        try:
            result = self._runner(
                command,
                input=encoded,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=CLI_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise OfficeSetupError("office_bridge_timeout") from None
        except OSError:
            raise OfficeSetupError("office_bridge_unavailable") from None
        if result.returncode != 0 or not isinstance(result.stdout, bytes):
            raise OfficeSetupError("office_bridge_rejected")
        if len(result.stdout) > MAX_CLI_OUTPUT_BYTES:
            raise OfficeSetupError("office_bridge_response_too_large")
        try:
            raw = result.stdout.decode("utf-8")
            one_line = raw.removesuffix("\n")
            if not one_line or "\n" in one_line or "\r" in one_line:
                raise ValueError("not one JSON line")
            document = json.loads(one_line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise OfficeSetupError("office_bridge_response_invalid") from None
        if not isinstance(document, dict):
            raise OfficeSetupError("office_bridge_response_invalid")
        return document


@dataclass(slots=True)
class DiscoverySnapshot:
    """Cache IDs returned by Exchange so the browser can select only observed targets."""

    ids: frozenset[str]
    site_ids: frozenset[str] = frozenset()
    kinds: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SetupSession:
    """Hold CSRF and bounded target-discovery evidence for one browser session."""

    csrf_token: str
    created_at: float = field(default_factory=time.monotonic)
    credential_revision: int | None = None
    enabled_features: dict[str, bool] | None = None
    discoveries: dict[str, DiscoverySnapshot] = field(default_factory=dict)
    callback_message: str | None = None


@dataclass(slots=True)
class ActiveFlow:
    """Keep nonsecret flow references while the OAuth code remains in callback memory only."""

    profile_ref: str
    link_session_ref: str
    expires_at: str
    created_at: float = field(default_factory=time.monotonic)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys before security-sensitive request validation."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _safe_identifier(value: Any, *, maximum: int = 512) -> str:
    """Validate one opaque provider identifier without interpreting it as a URL."""

    if not isinstance(value, str) or len(value) > maximum or not _SAFE_ID_RE.fullmatch(value):
        raise OfficeSetupError("office_identifier_invalid")
    return value


def _validate_authorization_url(
    value: Any,
    *,
    port: int,
    tenant_id: str,
    enabled_features: Mapping[str, bool],
) -> str:
    """Allow only the expected Microsoft authorization endpoint and loopback callback."""

    if not isinstance(value, str) or len(value) > 16_384:
        raise OfficeSetupError("office_authorization_url_invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True, max_num_fields=64)
    except (ValueError, UnicodeError):
        raise OfficeSetupError("office_authorization_url_invalid") from None
    path_parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _AUTHORIZATION_HOSTS
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or len(path_parts) != 4
        or path_parts[0].casefold() not in {tenant_id.casefold(), "organizations"}
        or path_parts[1:] != ["oauth2", "v2.0", "authorize"]
    ):
        raise OfficeSetupError("office_authorization_url_invalid")
    redirect_values = query.get("redirect_uri", [])
    allowed_redirects = {f"http://localhost:{port}{CALLBACK_PATH}"}
    if len(redirect_values) != 1 or redirect_values[0] not in allowed_redirects:
        raise OfficeSetupError("office_callback_uri_invalid")
    state_values = query.get("state", [])
    if len(state_values) != 1:
        raise OfficeSetupError("office_authorization_state_missing")
    state = state_values[0]
    if (
        not state
        or len(state.encode("utf-8")) > _MAX_OAUTH_STATE_BYTES
        or any(ord(character) < 33 or ord(character) > 126 for character in state)
    ):
        raise OfficeSetupError("office_authorization_state_invalid")
    client_ids = query.get("client_id", [])
    if len(client_ids) != 1 or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", client_ids[0]):
        raise OfficeSetupError("office_authorization_client_invalid")
    if len(query.get("response_type", [])) != 1 or query["response_type"] != ["code"]:
        raise OfficeSetupError("office_authorization_response_type_invalid")
    if len(query.get("response_mode", [])) != 1 or query["response_mode"] != ["query"]:
        raise OfficeSetupError("office_authorization_response_mode_invalid")
    if (
        len(query.get("code_challenge_method", [])) != 1
        or query["code_challenge_method"] != ["S256"]
        or len(query.get("code_challenge", [])) != 1
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", query["code_challenge"][0])
    ):
        raise OfficeSetupError("office_pkce_invalid")
    requested_scopes = query.get("scope", [])
    if len(requested_scopes) != 1:
        raise OfficeSetupError("office_authorization_scopes_invalid")
    scopes = requested_scopes[0].split()
    if any(_is_mail_scope(name) for name in scopes):
        raise OfficeSetupError("office_mail_scope_forbidden")
    _validate_scope_policy(scopes, enabled_features)
    if any(
        name.casefold() in {"code", "access_token", "refresh_token", "client_secret"}
        for name in query
    ):
        raise OfficeSetupError("office_authorization_url_invalid")
    return value


def _is_mail_scope(scope: str) -> bool:
    """Detect Mail permissions in short names and full Graph permission URIs."""

    return scope.casefold().rsplit("/", 1)[-1].startswith("mail.")


def _scope_name(scope: str) -> str:
    """Normalize a Graph scope URI to its permission name for exact policy checks."""

    return scope.casefold().rsplit("/", 1)[-1]


def _validate_scope_policy(scopes: Sequence[str], enabled_features: Mapping[str, bool]) -> None:
    """Require only the OAuth permissions derived from the fixed host feature flags."""

    names = {_scope_name(scope) for scope in scopes}
    expected = {"user.read", "offline_access"}
    if enabled_features["plannerEnabled"] or enabled_features["todoEnabled"]:
        expected.add("tasks.readwrite")
    if enabled_features["calendarEnabled"]:
        expected.add("calendars.readwrite")
    if enabled_features["sharePointDriveMetadataEnabled"]:
        expected.add("sites.selected")
    allowed = expected | {"openid", "profile", "email"}
    if not expected.issubset(names) or names - allowed:
        raise OfficeSetupError("office_oauth_scope_policy_invalid")


def _safe_status(document: Mapping[str, Any], operator_context: OperatorContext) -> dict[str, Any]:
    """Validate Exchange status and return only display-safe identity and scope state."""

    required = {
        "state",
        "profileRef",
        "provider",
        "authStatus",
        "accessMode",
        "externalSubject",
        "scopeSchemaVersion",
        "targetScope",
        "requiredOAuthScopes",
        "credentialRevision",
        "credentialExpiresAt",
        "refreshExpiresAt",
        "reauthRequired",
        "reauthReasonCode",
        "enabledFeatures",
        "targetRefCount",
    }
    if set(document) != required:
        raise OfficeSetupError("office_status_invalid")
    if document.get("provider") != _EXCHANGE_PROVIDER_ID:
        raise OfficeSetupError("office_profile_provider_mismatch")
    for field_name in ("state", "authStatus", "accessMode"):
        if not isinstance(document.get(field_name), str) or not _SAFE_CODE_RE.fullmatch(
            document[field_name]
        ):
            raise OfficeSetupError("office_status_invalid")
    profile_ref = document.get("profileRef")
    if not isinstance(profile_ref, str) or not _SAFE_ID_RE.fullmatch(profile_ref):
        raise OfficeSetupError("office_status_invalid")
    external_subject = document.get("externalSubject")
    if external_subject is not None and (
        not isinstance(external_subject, str)
        or len(external_subject) > 128
        or not re.fullmatch(r"[0-9a-fA-F-]{36}:[0-9a-fA-F-]{36}", external_subject)
    ):
        raise OfficeSetupError("office_status_identity_invalid")
    if document.get("scopeSchemaVersion") != 2:
        raise OfficeSetupError("office_status_invalid")
    enabled_features = document.get("enabledFeatures")
    if (
        not isinstance(enabled_features, dict)
        or set(enabled_features) != OFFICE_FEATURE_KEYS
        or any(not isinstance(value, bool) for value in enabled_features.values())
    ):
        raise OfficeSetupError("office_status_features_invalid")
    if any(
        enabled_features[name]
        for name in ("mailMetadataEnabled", "mailDraftsEnabled", "mailSendEnabled")
    ):
        raise OfficeSetupError("office_mail_scope_forbidden")
    scopes = document.get("requiredOAuthScopes")
    if (
        not isinstance(scopes, list)
        or len(scopes) > 32
        or any(
            not isinstance(scope, str) or not _SAFE_SCOPE_RE.fullmatch(scope) for scope in scopes
        )
    ):
        raise OfficeSetupError("office_status_scopes_invalid")
    if any(_is_mail_scope(scope) for scope in scopes):
        raise OfficeSetupError("office_mail_scope_forbidden")
    _validate_scope_policy(scopes, enabled_features)
    revision = document.get("credentialRevision")
    if revision is not None and (
        not isinstance(revision, int) or isinstance(revision, bool) or revision < 0
    ):
        raise OfficeSetupError("office_status_revision_invalid")
    target_ref_count = document.get("targetRefCount")
    if (
        not isinstance(target_ref_count, int)
        or isinstance(target_ref_count, bool)
        or target_ref_count < 0
    ):
        raise OfficeSetupError("office_status_targets_invalid")
    if not isinstance(document.get("reauthRequired"), bool):
        raise OfficeSetupError("office_status_invalid")
    for field_name in ("credentialExpiresAt", "refreshExpiresAt", "reauthReasonCode"):
        value = document.get(field_name)
        if value is not None and (not isinstance(value, str) or len(value) > 128):
            raise OfficeSetupError("office_status_invalid")
    target_scope = document.get("targetScope")
    if target_scope is not None and not isinstance(target_scope, dict):
        raise OfficeSetupError("office_status_invalid")
    identity_verified = (
        document["authStatus"] == "healthy"
        and document["accessMode"] == "actor_owned"
        and external_subject == operator_context.external_subject
        and revision is not None
        and isinstance(target_scope, dict)
        and target_scope.get("workspaceId") == operator_context.workspace_id
        and document["reauthRequired"] is False
    )
    return {
        "state": document["state"],
        "provider": document["provider"],
        "authStatus": document["authStatus"],
        "accessMode": document["accessMode"],
        "identityVerified": identity_verified and document["reauthRequired"] is False,
        "scopeSchemaVersion": document["scopeSchemaVersion"],
        "enabledFeatures": {
            key: value for key, value in enabled_features.items() if not key.startswith("mail")
        },
        "targetRefCount": target_ref_count,
        "requiredOAuthScopes": list(scopes),
        "credentialRevision": revision,
        "credentialExpiresAt": document["credentialExpiresAt"],
        "refreshExpiresAt": document["refreshExpiresAt"],
        "reauthRequired": document["reauthRequired"],
        "reauthReasonCode": document["reauthReasonCode"],
    }


def _validate_discovery(
    document: Mapping[str, Any],
    resource_type: str,
    *,
    page_cursor: str | None,
    credential_revision: int,
) -> tuple[list[dict[str, str]], DiscoverySnapshot, str | None, str | None, bool]:
    """Validate one Exchange page and retain only bounded target metadata."""

    expected_keys = {
        "state",
        "resourceType",
        "items",
        "pageCursor",
        "nextCursor",
        "complete",
        "truncated",
        "credentialRevision",
    }
    allowed_keys = expected_keys | ({"siteId"} if resource_type == "sharepoint_drives" else set())
    if set(document) - allowed_keys or not expected_keys.issubset(document):
        raise OfficeSetupError("office_discovery_invalid")
    if document.get("state") != "DISCOVERED" or document.get("resourceType") != resource_type:
        raise OfficeSetupError("office_discovery_invalid")
    if document.get("pageCursor") != page_cursor:
        raise OfficeSetupError("office_discovery_cursor_mismatch")
    next_cursor = document.get("nextCursor")
    if next_cursor is not None and (
        not isinstance(next_cursor, str)
        or len(next_cursor) > 12_000
        or not next_cursor
        or any(ord(character) < 33 or ord(character) > 126 for character in next_cursor)
    ):
        raise OfficeSetupError("office_discovery_cursor_invalid")
    complete = document.get("complete")
    truncated = document.get("truncated")
    if (
        not isinstance(complete, bool)
        or not isinstance(truncated, bool)
        or truncated
        or complete != (next_cursor is None)
    ):
        raise OfficeSetupError("office_discovery_incomplete")
    revision = document.get("credentialRevision")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision != credential_revision
    ):
        raise OfficeSetupError("office_discovery_revision_conflict")
    raw_site_id = document.get("siteId")
    site_id = _safe_identifier(raw_site_id) if raw_site_id is not None else None
    items = document.get("items")
    if not isinstance(items, list) or len(items) > 500:
        raise OfficeSetupError("office_discovery_invalid")
    safe_items: list[dict[str, str]] = []
    ids: set[str] = set()
    site_ids: set[str] = set()
    kinds: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or not {"id", "displayName"}.issubset(item):
            raise OfficeSetupError("office_discovery_item_invalid")
        if resource_type == "sharepoint_drives":
            if set(item) - {"id", "displayName", "driveType", "siteId", "kind"}:
                raise OfficeSetupError("office_discovery_item_invalid")
            drive_type = item.get("driveType") or item.get("kind")
            item_site_id_value = item.get("siteId") or site_id
            if (
                site_id is not None
                and item_site_id_value is not None
                and item_site_id_value != site_id
            ):
                raise OfficeSetupError("office_discovery_item_invalid")
            item_site_id = (
                _safe_identifier(item_site_id_value) if item_site_id_value is not None else None
            )
            if drive_type is None:
                raise OfficeSetupError("office_discovery_item_invalid")
            kind = "list" if str(drive_type).casefold() == "list" else "drive"
        else:
            if set(item) - {"id", "displayName", "kind", "webUrl"} or "kind" not in item:
                raise OfficeSetupError("office_discovery_item_invalid")
            item_site_id = None
            kind = item["kind"]
        item_id = _safe_identifier(item["id"])
        display_name = item["displayName"]
        if (
            not isinstance(display_name, str)
            or not display_name.strip()
            or len(display_name) > 256
            or any(ord(character) < 32 for character in display_name)
            or not isinstance(kind, str)
            or not _SAFE_CODE_RE.fullmatch(kind)
            or item_id in ids
        ):
            raise OfficeSetupError("office_discovery_item_invalid")
        ids.add(item_id)
        kinds[item_id] = kind
        safe = {"id": item_id, "displayName": display_name, "kind": kind}
        if item_site_id is not None:
            site_ids.add(item_site_id)
            safe["siteId"] = item_site_id
        safe_items.append(safe)
    if site_id:
        site_ids.add(site_id)
    if resource_type == "sharepoint_drives" and safe_items and not site_ids:
        raise OfficeSetupError("office_discovery_site_missing")
    return (
        safe_items,
        DiscoverySnapshot(frozenset(ids), frozenset(site_ids), kinds),
        site_id,
        next_cursor,
        complete,
    )


def _validate_targets(value: Any) -> dict[str, Any]:
    """Accept IDs only; feature flags, tenant, Workspace and OAuth scopes are never browser inputs."""

    expected_keys = {
        "plannerPlanIds",
        "sharePointListIds",
        "sharePointDriveIds",
        "toDoTaskListIds",
        "calendarIds",
        "freebusySubjects",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise OfficeSetupError("office_targets_invalid")
    result: dict[str, Any] = {}
    for key in expected_keys:
        rows = value[key]
        if (
            not isinstance(rows, list)
            or len(rows) > 100
            or any(not isinstance(item, str) for item in rows)
            or len(set(rows)) != len(rows)
        ):
            raise OfficeSetupError("office_targets_invalid")
        result[key] = [_safe_identifier(item) for item in rows]
    return result


class OfficeSetupService:
    """Coordinate one operator OAuth link and bounded metadata target selection."""

    def __init__(
        self,
        bridge: OfficeSetupBridge,
        *,
        operator_context: OperatorContext,
        port: int = DEFAULT_PORT,
        session_ttl: int = SESSION_TTL_SECONDS,
    ) -> None:
        self.bridge = bridge
        self.operator_context = operator_context
        self.port = port
        self.session_ttl = session_ttl
        self._lock = threading.RLock()
        self._sessions: dict[str, SetupSession] = {}
        self._active_flow: ActiveFlow | None = None
        self._last_callback_message: str | None = None
        self._profile_ref: str | None = None

    def create_session(self) -> tuple[str, SetupSession]:
        """Create an isolated CSRF session and evict expired setup pages."""

        with self._lock:
            self._evict_sessions()
            if len(self._sessions) >= MAX_SESSIONS:
                raise OfficeSetupError("office_setup_busy")
            session_id = secrets.token_urlsafe(32)
            session = SetupSession(csrf_token=secrets.token_urlsafe(32))
            self._sessions[session_id] = session
            return session_id, session

    def get_session(self, session_id: str | None) -> SetupSession:
        """Return only a current, known browser session."""

        if not isinstance(session_id, str) or not session_id:
            raise OfficeSetupError("session_required")
        with self._lock:
            self._evict_sessions()
            session = self._sessions.get(session_id)
            if session is None:
                raise OfficeSetupError("session_expired")
            return session

    def _evict_sessions(self) -> None:
        """Remove expired sessions without retaining browser state indefinitely."""

        now = time.monotonic()
        for session_id, session in list(self._sessions.items()):
            if now - session.created_at > self.session_ttl:
                self._sessions.pop(session_id, None)

    def begin(self, session: SetupSession) -> str:
        """Begin the fixed Exchange OAuth flow and return its URL only to the redirect handler."""

        with self._lock:
            if (
                self._active_flow
                and time.monotonic() - self._active_flow.created_at < self.session_ttl
            ):
                raise OfficeSetupError("office_authorization_pending")
            current = _safe_status(self.bridge.invoke("status", {}), self.operator_context)
            session.credential_revision = current["credentialRevision"]
            session.enabled_features = current["enabledFeatures"]
            if current["identityVerified"] and not current["reauthRequired"]:
                raise OfficeSetupError("office_already_linked")
            response = self.bridge.invoke("begin", {})
            required = {
                "state",
                "provider",
                "profileRef",
                "linkSessionRef",
                "authorizationUrl",
                "expiresAt",
            }
            if set(response) != required or response.get("provider") != _EXCHANGE_PROVIDER_ID:
                raise OfficeSetupError("office_begin_response_invalid")
            if response.get("state") != "AUTHORIZATION_REQUIRED":
                raise OfficeSetupError("office_begin_state_invalid")
            profile_ref = _safe_identifier(response.get("profileRef"))
            link_ref = _safe_identifier(response.get("linkSessionRef"))
            expires_at = response.get("expiresAt")
            if not isinstance(expires_at, str) or len(expires_at) > 128:
                raise OfficeSetupError("office_begin_response_invalid")
            auth_url = _validate_authorization_url(
                response.get("authorizationUrl"),
                port=self.port,
                tenant_id=self.operator_context.tenant_id,
                enabled_features=current["enabledFeatures"],
            )
            self._active_flow = ActiveFlow(profile_ref, link_ref, expires_at)
            self._last_callback_message = "AUTHORIZATION_PENDING"
            return auth_url

    def complete_callback(self, query: str) -> None:
        """Pass one loopback callback's code/state directly to the Exchange CLI stdin."""

        try:
            values = urllib.parse.parse_qs(
                query, strict_parsing=True, keep_blank_values=True, max_num_fields=8
            )
        except ValueError:
            raise OfficeSetupError("office_callback_invalid") from None
        if any(len(rows) != 1 for rows in values.values()):
            raise OfficeSetupError("office_callback_invalid")
        state = values.get("state", [None])[0]
        code = values.get("code", [None])[0]
        allowed_keys = {
            "state",
            "code",
            "session_state",
            "client_info",
            "iss",
            "scope",
            "error",
            "error_description",
            "error_uri",
        }
        if set(values) - allowed_keys:
            raise OfficeSetupError("office_callback_invalid")
        if any(
            len(value.encode("utf-8")) > _MAX_OAUTH_CODE_BYTES
            or any(ord(character) < 32 for character in value)
            for rows in values.values()
            for value in rows
        ):
            raise OfficeSetupError("office_callback_invalid")
        issuer_values = values.get("iss", [])
        if issuer_values and issuer_values != [self.operator_context.issuer]:
            raise OfficeSetupError("office_callback_issuer_invalid")
        callback_scopes = [name for scope in values.get("scope", []) for name in scope.split()]
        if any(_is_mail_scope(name) for name in callback_scopes):
            raise OfficeSetupError("office_mail_scope_forbidden")
        if "error" in values:
            if (
                "code" in values
                or not isinstance(state, str)
                or not state
                or len(state.encode("utf-8")) > _MAX_OAUTH_STATE_BYTES
                or any(ord(character) < 33 or ord(character) > 126 for character in state)
            ):
                raise OfficeSetupError("office_callback_invalid")
            self.callback_failed()
            return
        if not isinstance(state, str) or not isinstance(code, str):
            raise OfficeSetupError("office_callback_invalid")
        if (
            len(state.encode("utf-8")) > _MAX_OAUTH_STATE_BYTES
            or len(code.encode("utf-8")) > _MAX_OAUTH_CODE_BYTES
            or not state
            or not code
            or any(ord(character) < 33 or ord(character) > 126 for character in state + code)
        ):
            raise OfficeSetupError("office_callback_invalid")
        # Code and state are never stored, placed in argv, or returned to the browser.
        response = self.bridge.invoke("complete", {"state": state, "code": code})
        safe = _safe_status(response, self.operator_context)
        if not safe["identityVerified"]:
            raise OfficeSetupError("office_owner_identity_mismatch")
        with self._lock:
            if self._active_flow and safe.get("provider") != _EXCHANGE_PROVIDER_ID:
                raise OfficeSetupError("office_callback_profile_mismatch")
            if self._active_flow and response.get("profileRef") != self._active_flow.profile_ref:
                raise OfficeSetupError("office_callback_profile_mismatch")
        self._active_flow = None
        self._last_callback_message = "AUTHORIZATION_COMPLETE"

    def callback_failed(self) -> None:
        """Forget any pending UI flow after a provider error without retaining its query."""

        with self._lock:
            self._active_flow = None
            self._last_callback_message = "AUTHORIZATION_DID_NOT_COMPLETE"

    def status(self, session: SetupSession) -> dict[str, Any]:
        """Read Exchange's owner-bound profile state and expose only safe status fields."""

        response = self.bridge.invoke("status", {})
        safe = _safe_status(response, self.operator_context)
        with self._lock:
            session.credential_revision = safe["credentialRevision"]
            session.enabled_features = safe["enabledFeatures"]
            self._profile_ref = response["profileRef"]
            if self._active_flow and response.get("profileRef") != self._active_flow.profile_ref:
                raise OfficeSetupError("office_status_profile_mismatch")
            safe["message"] = self._last_callback_message or "READY"
        return safe

    def discover(self, session: SetupSession, resource_type: str) -> dict[str, Any]:
        """List only fixed metadata resources returned by Exchange's owner-bound discovery."""

        if resource_type not in ALLOWED_DISCOVERY_TYPES:
            raise OfficeSetupError("office_discovery_type_invalid")
        status = self.status(session)
        if status["reauthRequired"] or not status["identityVerified"]:
            raise OfficeSetupError("office_identity_not_ready")
        feature = {
            "planner_plans": "plannerEnabled",
            "todo_lists": "todoEnabled",
            "sharepoint_drives": "sharePointDriveMetadataEnabled",
            "calendars": "calendarEnabled",
            "freebusy_subjects": "calendarEnabled",
        }[resource_type]
        if not status["enabledFeatures"][feature]:
            raise OfficeSetupError("office_feature_disabled")
        if status["credentialRevision"] is None:
            raise OfficeSetupError("office_identity_not_ready")
        next_cursor: str | None = None
        seen_cursors: set[str] = set()
        all_items: list[dict[str, str]] = []
        all_ids: set[str] = set()
        all_sites: set[str] = set()
        all_kinds: dict[str, str] = {}
        for _page_number in range(MAX_DISCOVERY_PAGES):
            request: dict[str, Any] = {"resourceType": resource_type}
            if next_cursor is not None:
                request["pageCursor"] = next_cursor
            response = self.bridge.invoke("discover", request)
            items, snapshot, _site_id, following_cursor, complete = _validate_discovery(
                response,
                resource_type,
                page_cursor=next_cursor,
                credential_revision=status["credentialRevision"],
            )
            if all_ids.intersection(snapshot.ids):
                raise OfficeSetupError("office_discovery_duplicate_id")
            all_ids.update(snapshot.ids)
            all_items.extend(items)
            all_sites.update(snapshot.site_ids)
            all_kinds.update(snapshot.kinds)
            if len(all_items) > MAX_DISCOVERY_ITEMS:
                raise OfficeSetupError("office_discovery_too_many_items")
            if complete:
                break
            if following_cursor is None or following_cursor in seen_cursors:
                raise OfficeSetupError("office_discovery_cursor_invalid")
            seen_cursors.add(following_cursor)
            next_cursor = following_cursor
        else:
            raise OfficeSetupError("office_discovery_page_limit")
        if len(all_sites) > 1:
            raise OfficeSetupError("office_discovery_site_mismatch")
        combined = DiscoverySnapshot(frozenset(all_ids), frozenset(all_sites), all_kinds)
        with self._lock:
            session.discoveries[resource_type] = combined
        result: dict[str, Any] = {"resourceType": resource_type, "items": all_items}
        if all_sites:
            result["siteId"] = next(iter(all_sites))
        return result

    def select_targets(
        self,
        session: SetupSession,
        *,
        expected_revision: Any,
        targets_value: Any,
    ) -> dict[str, Any]:
        """CAS-update only IDs observed by this session; feature and OAuth scopes stay fixed."""

        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or session.credential_revision is None
            or expected_revision != session.credential_revision
        ):
            raise OfficeSetupError("office_target_revision_conflict")
        latest = self.status(session)
        if latest["credentialRevision"] != expected_revision:
            raise OfficeSetupError("office_target_revision_conflict")
        if latest["reauthRequired"] or not latest["identityVerified"]:
            raise OfficeSetupError("office_identity_not_ready")
        targets = _validate_targets(targets_value)
        self._validate_target_features(targets, latest["enabledFeatures"])
        self._validate_discovery_selection(session, targets)
        freebusy = session.discoveries.get("freebusy_subjects")
        if freebusy is not None and freebusy.ids and not targets["freebusySubjects"]:
            raise OfficeSetupError("office_freebusy_target_required")
        response = self.bridge.invoke(
            "select-targets",
            {"expectedCredentialRevision": expected_revision, "targets": targets},
        )
        if response.get("state") == "CONFLICT":
            raise OfficeSetupError("office_target_revision_conflict")
        if response.get("state") != "UPDATED":
            raise OfficeSetupError("office_target_update_invalid")
        updated = _safe_status(response, self.operator_context)
        if response.get("profileRef") != self._profile_ref:
            raise OfficeSetupError("office_target_update_profile_mismatch")
        revision = updated["credentialRevision"]
        if not isinstance(revision, int) or revision != expected_revision + 1:
            raise OfficeSetupError("office_target_update_invalid")
        if (
            updated["requiredOAuthScopes"] != latest["requiredOAuthScopes"]
            or updated["enabledFeatures"] != latest["enabledFeatures"]
            or not updated["identityVerified"]
        ):
            raise OfficeSetupError("office_target_update_scope_invalid")
        with self._lock:
            session.credential_revision = revision
        return {
            "state": "UPDATED",
            "credentialRevision": revision,
            "scopeSchemaVersion": 2,
            "requiredOAuthScopes": list(updated["requiredOAuthScopes"]),
            "selectedCounts": {
                "plannerPlans": len(targets["plannerPlanIds"]),
                "toDoLists": len(targets["toDoTaskListIds"]),
                "calendars": len(targets["calendarIds"]),
                "sharePointLists": len(targets["sharePointListIds"]),
                "sharePointLibraries": len(targets["sharePointDriveIds"]),
                "freebusySubjects": len(targets["freebusySubjects"]),
            },
        }

    @staticmethod
    def _validate_discovery_selection(session: SetupSession, targets: Mapping[str, Any]) -> None:
        """Require every user-selected target to come from a prior fixed discovery result."""

        checks = {
            "plannerPlanIds": "planner_plans",
            "toDoTaskListIds": "todo_lists",
            "calendarIds": "calendars",
            "freebusySubjects": "freebusy_subjects",
        }
        for target_key, resource_type in checks.items():
            selected = set(targets[target_key])
            observed = session.discoveries.get(resource_type)
            if selected and (observed is None or not selected.issubset(observed.ids)):
                raise OfficeSetupError("office_target_not_discovered")
        drives = session.discoveries.get("sharepoint_drives")
        selected_drives = set(targets["sharePointDriveIds"])
        selected_lists = set(targets["sharePointListIds"])
        if (selected_drives or selected_lists) and drives is None:
            raise OfficeSetupError("office_target_not_discovered")
        if drives is not None:
            if not selected_drives.issubset(
                {
                    item
                    for item, kind in drives.kinds.items()
                    if kind.casefold() in {"drive", "library"}
                }
            ):
                raise OfficeSetupError("office_target_not_discovered")
            if not selected_lists.issubset(
                {item for item, kind in drives.kinds.items() if kind.casefold() == "list"}
            ):
                raise OfficeSetupError("office_target_not_discovered")

    @staticmethod
    def _validate_target_features(
        targets: Mapping[str, Any], enabled_features: Mapping[str, bool]
    ) -> None:
        """Reject any target list whose fixed server-side Office capability is disabled."""

        target_feature = {
            "plannerPlanIds": "plannerEnabled",
            "toDoTaskListIds": "todoEnabled",
            "calendarIds": "calendarEnabled",
            "freebusySubjects": "calendarEnabled",
            "sharePointListIds": "sharePointDriveMetadataEnabled",
            "sharePointDriveIds": "sharePointDriveMetadataEnabled",
        }
        if any(
            targets[key] and not enabled_features[feature]
            for key, feature in target_feature.items()
        ):
            raise OfficeSetupError("office_feature_disabled")


def _session_cookie_id(headers: Mapping[str, str]) -> str | None:
    """Read one host-only browser session cookie without accepting duplicate values."""

    found: str | None = None
    for item in headers.get("Cookie", "").split(";"):
        name, separator, value = item.strip().partition("=")
        if separator and name == SESSION_COOKIE:
            if found is not None:
                raise OfficeSetupError("session_invalid")
            found = value
    return found


def _is_loopback_host_header(value: str, port: int) -> bool:
    """Accept only the two intended loopback DNS spellings on the bound port."""

    try:
        parsed = urllib.parse.urlsplit(f"//{value}")
        host = parsed.hostname
        request_port = parsed.port
    except ValueError:
        return False
    return (
        host in {"127.0.0.1", "localhost"}
        and request_port == port
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == ""
    )


def _is_same_origin(value: str, port: int, host_header: str | None = None) -> bool:
    """Require an exact HTTP localhost or 127.0.0.1 origin and no URL suffix."""

    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    matches = (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and parsed.port == port
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == ""
    )
    if not matches or host_header is None:
        return matches
    try:
        host = urllib.parse.urlsplit(f"//{host_header}")
        return host.hostname == parsed.hostname and host.port == parsed.port
    except ValueError:
        return False


def _safe_peer_address(value: str) -> str | None:
    """Normalize a TCP peer address without trusting proxy headers."""

    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address)
    except ValueError:
        return None


def _page(*, nonce: str, csrf: str) -> bytes:
    """Build the closed, same-origin page without embedding credentials or OAuth state."""

    safe_csrf = json.dumps(csrf)
    document = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cyrene Office connection</title></head><body>
<main><h1>Connect Microsoft 365</h1>
<p>This private setup page links the configured DoHorizon owner account. Microsoft manages sign-in; Cyrene stores OAuth credentials in Account Center.</p>
<p id="status" role="status">Checking the existing connection…</p>
<form id="authorize" method="post" action="/authorize" target="cyrene-office-oauth">
  <input type="hidden" name="csrf" value="__CSRF__">
  <button id="connect" type="submit">Sign in with Microsoft</button>
</form>
<section id="targets" hidden>
  <h2>Choose allowed Office targets</h2>
  <p>The server limits this list to resources available to the configured account and Workspace.</p>
  <div id="resource-panels"></div>
  <button id="discover" type="button">Load available targets</button>
  <button id="save" type="button" disabled>Save selected targets</button>
  <p id="selection" role="status"></p>
</section></main>
<script nonce="__NONCE__">
const csrf=__CSRF_JSON__;
const statusNode=document.getElementById('status');
const panels=document.getElementById('resource-panels');
const groups=[
  ['planner_plans','Planner plans','plannerPlanIds'],
  ['todo_lists','To Do lists','toDoTaskListIds'],
  ['calendars','Calendars','calendarIds'],
  ['sharepoint_drives','SharePoint document libraries','sharePointResources'],
  ['freebusy_subjects','Free/busy people','freebusySubjects']
];
let revision=null;
let discovered={};
let selected={};
let sharePointSiteId=null;
async function api(path,payload){
  const response=await fetch(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Cyrene-CSRF':csrf},body:JSON.stringify({...payload,csrf})});
  const data=await response.json();
  if(!response.ok) throw new Error(data.error||'office_setup_failed');
  return data;
}
function renderGroup(type,title,items){
  const section=document.createElement('section');
  const heading=document.createElement('h3'); heading.textContent=title; section.append(heading);
  if(!items.length){const empty=document.createElement('p');empty.textContent='No allowed resources were found.';section.append(empty);}
  for(const item of items){
    const label=document.createElement('label');
    const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.value=item.id;
    checkbox.dataset.resource=type;checkbox.checked=(selected[type]||[]).includes(item.id);
    const name=document.createElement('span');name.textContent=item.displayName;
    const kind=document.createElement('small');kind.textContent=' '+item.kind;
    label.append(checkbox,name,kind);
    if(item.siteId){const site=document.createElement('small');site.textContent=' · site '+item.siteId;label.append(site);}
    section.append(label,document.createElement('br'));
  }
  panels.append(section);
}
function renderAll(){
  panels.replaceChildren();
  if(sharePointSiteId){const site=document.createElement('p');site.textContent='Allowed SharePoint site: '+sharePointSiteId;panels.append(site);}
  for(const [type,title] of groups) if(discovered[type]) renderGroup(type,title,discovered[type]);
  document.getElementById('save').disabled=!Object.keys(discovered).length;
}
async function refresh(){
  try{
    const response=await fetch('/api/status',{credentials:'same-origin'});
    const data=await response.json();
    if(!response.ok) throw new Error(data.error||'office_status_unavailable');
    revision=data.credentialRevision;
    statusNode.textContent=data.identityVerified
      ? `Microsoft account verified · ${data.authStatus} · credential revision ${revision}`
      : data.message==='AUTHORIZATION_DID_NOT_COMPLETE'
        ? 'Microsoft sign-in did not complete. You can try again.'
        : `Microsoft connection status: ${data.authStatus}`;
    const ready=data.identityVerified&&!data.reauthRequired;
    document.getElementById('connect').disabled=ready;
    document.getElementById('targets').hidden=!ready;
    if(ready && data.requiredOAuthScopes.some(scope=>scope.toLowerCase().split('/').pop().startsWith('mail.'))){
      throw new Error('office_mail_scope_forbidden');
    }
  }catch(error){statusNode.textContent=error.message;}
}
document.getElementById('authorize').addEventListener('submit',event=>{
  const popup=window.open('about:blank','cyrene-office-oauth','popup,width=720,height=820');
  if(!popup){event.preventDefault();statusNode.textContent='Allow the Microsoft sign-in window, then try again.';return;}
  statusNode.textContent='Waiting for Microsoft sign-in…';
});
document.getElementById('discover').addEventListener('click',async()=>{
  document.getElementById('discover').disabled=true;
  const errors=[];
  try{
    for(const [type] of groups){
      try{
        const result=await api('/api/discover',{resourceType:type});
        discovered[type]=result.items;
        if(type==='sharepoint_drives') sharePointSiteId=result.siteId||(result.items.find(item=>item.siteId)||{}).siteId||null;
      }catch(error){discovered[type]=[];errors.push(type);}
    }
    renderAll();
    document.getElementById('selection').textContent=errors.length
      ? `Some resource lists are unavailable (${errors.join(', ')}). Review available items and save the selection.`
      : 'Review the listed items and save your target selection.';
  }catch(error){document.getElementById('selection').textContent=error.message;}
  finally{document.getElementById('discover').disabled=false;}
});
document.getElementById('save').addEventListener('click',async()=>{
  const values={plannerPlanIds:[],sharePointListIds:[],sharePointDriveIds:[],toDoTaskListIds:[],calendarIds:[],freebusySubjects:[]};
  const byType={planner_plans:'plannerPlanIds',todo_lists:'toDoTaskListIds',calendars:'calendarIds',freebusy_subjects:'freebusySubjects'};
  for(const checkbox of panels.querySelectorAll('input[type=checkbox]:checked')){
    const type=checkbox.dataset.resource;
    if(type==='sharepoint_drives'){
      const item=(discovered[type]||[]).find(row=>row.id===checkbox.value);
      if(!item) continue;
      if(item.kind.toLowerCase()==='list') values.sharePointListIds.push(item.id);
      else values.sharePointDriveIds.push(item.id);
    }else if(byType[type]) values[byType[type]].push(checkbox.value);
  }
  if((discovered.freebusy_subjects||[]).length&&!values.freebusySubjects.length){
    document.getElementById('selection').textContent='Keep at least one free/busy person selected for the configured calendar capability.';
    return;
  }
  try{
    const result=await api('/api/select-targets',{expectedCredentialRevision:revision,targets:values});
    revision=result.credentialRevision;
    document.getElementById('selection').textContent=`Saved selection at revision ${revision}. Planner ${result.selectedCounts.plannerPlans}; To Do ${result.selectedCounts.toDoLists}; calendars ${result.selectedCounts.calendars}; SharePoint libraries ${result.selectedCounts.sharePointLibraries}.`;
  }catch(error){document.getElementById('selection').textContent=error.message;}
});
refresh();
setInterval(refresh,5000);
</script></body></html>"""
    return (
        document.replace("__NONCE__", nonce)
        .replace("__CSRF__", csrf)
        .replace("__CSRF_JSON__", safe_csrf)
        .encode("utf-8")
    )


class OfficeSetupServer(ThreadingHTTPServer):
    """Serve the setup page on a temporary loopback-published container port."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        service: OfficeSetupService,
        *,
        trusted_peers: Sequence[str] = ("127.0.0.1", "::1"),
        server_nonce: str | None = None,
    ) -> None:
        self.service = service
        self.server_nonce = server_nonce or secrets.token_urlsafe(24)
        self.trusted_peers = frozenset(
            normalized
            for value in trusted_peers
            if (normalized := _safe_peer_address(value)) is not None
        )
        super().__init__(address, OfficeSetupHandler)
        self.expected_port = int(self.server_address[1])


class OfficeSetupHandler(BaseHTTPRequestHandler):
    """Enforce loopback Host/Origin, peer allowlist, session cookie, and CSRF."""

    server: OfficeSetupServer
    server_version = "CyreneOfficeSetup/1"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        """Suppress request-line and query logging because callbacks contain OAuth codes."""

    def send_error(
        self, _code: int, _message: str | None = None, _explain: str | None = None
    ) -> None:
        """Return a generic error without reflecting the request target or query string."""

        self._json(HTTPStatus.BAD_REQUEST, {"error": "office_request_invalid"})

    def _peer_valid(self) -> bool:
        peer = _safe_peer_address(str(self.client_address[0]))
        if peer is None:
            return False
        try:
            if ipaddress.ip_address(peer).is_loopback:
                return True
        except ValueError:
            return False
        return peer in self.server.trusted_peers

    def _host_valid(self) -> bool:
        return _is_loopback_host_header(self.headers.get("Host", ""), self.server.expected_port)

    def _origin_valid(self) -> bool:
        return _is_same_origin(
            self.headers.get("Origin", ""),
            self.server.expected_port,
            self.headers.get("Host", ""),
        )

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
        self.send_header("Content-Security-Policy", self._csp())
        self.end_headers()
        self.wfile.write(payload)

    def _csp(self) -> str:
        return (
            "default-src 'none'; connect-src 'self'; "
            f"script-src 'nonce-{self.server.server_nonce}'; "
            "style-src 'none'; form-action 'self'; base-uri 'none'; "
            "object-src 'none'; frame-ancestors 'none'"
        )

    def _json(self, status: int, value: Mapping[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _redirect(self, location: str = "/") -> None:
        body = b"OAuth response received. Return to the Office setup tab."
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _session(self, *, create: bool = False) -> tuple[str, SetupSession]:
        session_id = _session_cookie_id(self.headers)
        if create and not session_id:
            session_id, session = self.server.service.create_session()
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL_SECONDS}",
            )
            return session_id, session
        return session_id or "", self.server.service.get_session(session_id)

    def _cookie_session_id(self) -> str | None:
        try:
            return _session_cookie_id(self.headers)
        except OfficeSetupError:
            return None

    def _body(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise OfficeSetupError("content_type_invalid")
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or int(length) > MAX_BODY_BYTES:
            raise OfficeSetupError("request_body_invalid")
        try:
            body = json.loads(
                self.rfile.read(int(length)).decode("utf-8"), object_pairs_hook=_unique_object
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise OfficeSetupError("request_body_invalid") from None
        if not isinstance(body, dict):
            raise OfficeSetupError("request_body_invalid")
        return body

    def _authorize_json(self, body: Mapping[str, Any]) -> SetupSession:
        if not self._peer_valid() or not self._host_valid() or not self._origin_valid():
            raise OfficeSetupError("origin_not_allowed")
        session = self.server.service.get_session(self._cookie_session_id())
        csrf = body.get("csrf")
        header = self.headers.get("X-Cyrene-CSRF")
        if (
            not isinstance(csrf, str)
            or not isinstance(header, str)
            or not hmac.compare_digest(csrf, session.csrf_token)
            or not hmac.compare_digest(header, session.csrf_token)
        ):
            raise OfficeSetupError("csrf_invalid")
        return session

    def _authorize_form(self) -> tuple[str, SetupSession]:
        if not self._peer_valid() or not self._host_valid() or not self._origin_valid():
            raise OfficeSetupError("origin_not_allowed")
        content_type = self.headers.get_content_type()
        length = self.headers.get("Content-Length", "")
        if (
            content_type != "application/x-www-form-urlencoded"
            or not length.isdigit()
            or int(length) > 4096
        ):
            raise OfficeSetupError("request_body_invalid")
        try:
            values = urllib.parse.parse_qs(
                self.rfile.read(int(length)).decode("ascii"),
                strict_parsing=True,
                keep_blank_values=True,
                max_num_fields=4,
            )
        except (UnicodeDecodeError, ValueError):
            raise OfficeSetupError("request_body_invalid") from None
        if set(values) != {"csrf"} or len(values["csrf"]) != 1:
            raise OfficeSetupError("request_body_invalid")
        session = self.server.service.get_session(self._cookie_session_id())
        if not hmac.compare_digest(values["csrf"][0], session.csrf_token):
            raise OfficeSetupError("csrf_invalid")
        return values["csrf"][0], session

    def do_GET(self) -> None:
        if not self._peer_valid() or not self._host_valid():
            self._json(HTTPStatus.FORBIDDEN, {"error": "loopback_only"})
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == CALLBACK_PATH:
            try:
                self.server.service.complete_callback(parsed.query)
            except OfficeSetupError:
                self.server.service.callback_failed()
            self._redirect("/")
            return
        if parsed.path == "/":
            if parsed.query or parsed.fragment:
                self._redirect("/")
                return
            if urllib.parse.urlsplit(f"//{self.headers.get('Host', '')}").hostname == "127.0.0.1":
                self._redirect(f"http://localhost:{self.server.expected_port}/")
                return
            session_id = self._cookie_session_id()
            session: SetupSession | None = None
            if session_id:
                try:
                    session = self.server.service.get_session(session_id)
                except OfficeSetupError:
                    session = None
            if session is None:
                session_id, session = self.server.service.create_session()
                page = _page(
                    nonce=self.server.server_nonce,
                    csrf=session.csrf_token,
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("Content-Security-Policy", self._csp())
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL_SECONDS}",
                )
                self.end_headers()
                self.wfile.write(page)
                return
            page = _page(nonce=self.server.server_nonce, csrf=session.csrf_token)
            self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/status" and not parsed.query and not parsed.fragment:
            try:
                session = self.server.service.get_session(self._cookie_session_id())
                result = self.server.service.status(session)
                self._json(HTTPStatus.OK, result)
            except OfficeSetupError as error:
                status = (
                    HTTPStatus.UNAUTHORIZED
                    if str(error) in {"session_required", "session_expired"}
                    else HTTPStatus.BAD_GATEWAY
                )
                self._json(status, {"error": str(error)})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/authorize":
            try:
                _, session = self._authorize_form()
                authorization_url = self.server.service.begin(session)
                self._redirect(authorization_url)
            except OfficeSetupError as error:
                status = (
                    HTTPStatus.FORBIDDEN
                    if str(error) in {"origin_not_allowed", "csrf_invalid"}
                    else HTTPStatus.BAD_GATEWAY
                )
                self._json(status, {"error": str(error)})
            return
        try:
            body = self._body()
            session = self._authorize_json(body)
            if path == "/api/discover":
                if set(body) != {"csrf", "resourceType"} or not isinstance(
                    body.get("resourceType"), str
                ):
                    raise OfficeSetupError("request_body_invalid")
                result = self.server.service.discover(session, body["resourceType"])
                self._json(HTTPStatus.OK, result)
                return
            if path == "/api/select-targets":
                if set(body) != {"csrf", "expectedCredentialRevision", "targets"}:
                    raise OfficeSetupError("request_body_invalid")
                result = self.server.service.select_targets(
                    session,
                    expected_revision=body["expectedCredentialRevision"],
                    targets_value=body["targets"],
                )
                self._json(HTTPStatus.OK, result)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except OfficeSetupError as error:
            status = (
                HTTPStatus.FORBIDDEN
                if str(error) in {"origin_not_allowed", "csrf_invalid"}
                else HTTPStatus.UNAUTHORIZED
                if str(error) in {"session_required", "session_expired"}
                else HTTPStatus.BAD_REQUEST
            )
            self._json(status, {"error": str(error)})

    def do_OPTIONS(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})


def main(argv: list[str] | None = None) -> int:
    """Run the temporary owner OAuth page inside the restricted Exchange container."""

    parser = argparse.ArgumentParser(
        description="Serve the private Microsoft Office OAuth setup page on the temporary loopback port."
    )
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--trusted-peer",
        action="append",
        default=[],
        help="Exact host-side Docker bridge address permitted to reach the loopback-published port.",
    )
    args = parser.parse_args(argv)
    if args.port != DEFAULT_PORT:
        parser.error(f"the registered loopback redirect requires port {DEFAULT_PORT}")
    peers = list(args.trusted_peer)
    if args.host == "0.0.0.0" and not peers:
        parser.error("--trusted-peer is required when the container listener is not loopback-bound")
    if any(_safe_peer_address(peer) != peer for peer in peers):
        parser.error("--trusted-peer must contain canonical IP addresses")
    try:
        server = OfficeSetupServer(
            (args.host, args.port),
            OfficeSetupService(
                ExchangeCliBridge(),
                operator_context=load_private_operator_config(),
                port=args.port,
            ),
            trusted_peers=peers,
        )
    except OSError:
        parser.error("office loopback port is unavailable")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
