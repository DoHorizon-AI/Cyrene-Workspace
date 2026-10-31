"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 stage_transition.py                                                   │
│  Module: tooling.deploy.teams_cloud.stage_transition                     │
│  Role: Plan and apply one durable Teams cloud runtime transition.         │
│                                                                          │
│  模块职责：计划并执行可恢复的 Teams 云运行时正式迁移事务。                │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import preflight
import runtime_update
from build_arm64 import BuildError

PLAN_SCHEMA = "cyrene-teams-stage-transition-plan-v2"
JOURNAL_SCHEMA = "cyrene-teams-stage-transition-journal-v1"
SELECTION_SCHEMA = "cyrene-teams-runtime-selection-v2"
DEFAULT_JOURNAL_DIR = Path("/var/lib/cyrene/stage-transition")
OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE = "microsoft-graph-provider-bootstrap"
EXCHANGE_IMAGE_UPDATE_TARGET = "exchange"
PHASES = {
    "provider-bootstrap",
    OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE,
    "issued-connector-activation",
}
IMAGE_UPDATE_TARGETS = frozenset(
    {
        "account-center",
        "cloud-management-phase2",
        EXCHANGE_IMAGE_UPDATE_TARGET,
        "github-issues",
        "huggingface",
        "navigator",
        "navigator-persistence",
        "workspace-authority",
    }
)
ACCOUNT_CENTER_IMAGE_UPDATE_TARGET = "account-center"
ACCOUNT_CENTER_IMAGE_UPDATE_PHASES = frozenset(
    {OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE, "issued-connector-activation"}
)
WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET = "workspace-authority"
WORKSPACE_AUTHORITY_IMAGE_UPDATE_PHASES = frozenset(
    {OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE, "issued-connector-activation"}
)
CLOUD_IMAGE_UPDATE_TARGET = "cloud-management-phase2"
GITHUB_ISSUES_IMAGE_UPDATE_TARGET = "github-issues"
HUGGINGFACE_IMAGE_UPDATE_TARGET = "huggingface"
PERSISTENCE_IMAGE_UPDATE_TARGET = "navigator-persistence"
CLOUD_MONITOR_PROVIDER_IDS = frozenset({"azure", "gcp", "runpod"})
PROVIDER_LISTENER_REPAIR_TARGETS = ("huggingface", "cloud-management-phase2")
EXCHANGE_CLOUD_RUNNER_ENV = "CYRENE_EXCHANGE_AUTHORIZATION_CLOUD_RUNNER_ENABLED"
EXCHANGE_READINESS_IMAGE_TARGETS = frozenset(
    {
        ACCOUNT_CENTER_IMAGE_UPDATE_TARGET,
        "cloud-management-phase2",
        EXCHANGE_IMAGE_UPDATE_TARGET,
        GITHUB_ISSUES_IMAGE_UPDATE_TARGET,
        WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET,
    }
)
IMAGE_UPDATE_READINESS_TARGETS = EXCHANGE_READINESS_IMAGE_TARGETS | frozenset(
    {HUGGINGFACE_IMAGE_UPDATE_TARGET}
)
PRESERVE_NAVIGATOR_IMAGE_TARGETS = IMAGE_UPDATE_READINESS_TARGETS | frozenset(
    {PERSISTENCE_IMAGE_UPDATE_TARGET}
)
NEW_SERVICES = {"huggingface", "cloud-management-phase2"}
OPTIONAL_RUNTIME_SERVICE_ORDER = ("microsoft-graph", "azure-devops-observation")
SERVICE_ORDER = (*runtime_update.STAGE_TRANSITION_TARGET_ORDER, *OPTIONAL_RUNTIME_SERVICE_ORDER)
SERVICE_SET = set(SERVICE_ORDER)
OFFICE_GRAPH_STAGE_TARGET_ORDER = (
    "navigator-persistence",
    "account-center",
    "cloud-management-phase2",
    "microsoft-graph",
    "exchange",
    "teams-provider",
    "teams-ingress",
    "navigator",
)
OFFICE_PERSONAL_ROUTE_SECRET_NAMES = frozenset(
    {
        "teams-private-bridge-token",
        *(f"work-actor-token-{index:02d}" for index in range(1, 6)),
    }
)
# Exact long-running inventory for the active Teams cloud Compose profiles. Keep this
# separate from SERVICE_SET, which is only the six mutable targets in a staged update.
# 中文：这是完整运行拓扑；SERVICE_SET 仅表示六个可更新目标。
RUNTIME_SERVICE_ORDER = (
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
    "computer-runtime",
    "computer-runtime-proxy",
    "huggingface",
    "cloud-management-phase2",
)
RUNTIME_SERVICE_SET = frozenset(RUNTIME_SERVICE_ORDER)
OFFICE_GRAPH_RUNTIME_SERVICE_SET = RUNTIME_SERVICE_SET | {"microsoft-graph"}
ACCOUNT_CENTER_IMAGE_UPDATE_SERVICE_SET = OFFICE_GRAPH_RUNTIME_SERVICE_SET
ACCOUNT_CENTER_DATABASE_USER_VERSION = 9
ACCOUNT_CENTER_PROFILE_COLUMNS = (
    "profile_ref,anchor_issuer,anchor_subject,organization_id,workspace_id,provider,"
    "identity_ref,target_scope_json,auth_status,external_subject,credential_blob,"
    "credential_expires_at,credential_secret_ref,status,created_at,updated_at,auth_mode,"
    "credential_refresh_blob,credential_refresh_expires_at,oauth_client_id,oauth_scopes_json,"
    "refresh_claim_id,refresh_claim_owner,refresh_claimed_at,credential_revision,access_mode"
)
ACCOUNT_CENTER_PROFILE_COLUMN_NAMES = tuple(ACCOUNT_CENTER_PROFILE_COLUMNS.split(","))
ACCOUNT_CENTER_PROFILE_BLOB_COLUMN_INDICES = (10, 17)
ACCOUNT_CENTER_UNKNOWN_INVOCATION_COLUMNS = (
    "organization_id",
    "workspace_id",
    "actor_issuer",
    "actor_subject",
    "request_id",
    "connection_ref",
    "provider_id",
    "profile_ref",
    "operation_id",
    "resource_ref",
    "arguments_sha256",
    "envelope_sha256",
    "state",
    "operation_ref",
    "grant_ref",
    "is_mutating",
    "created_at",
    "expires_at",
    "response_json",
    "response_status",
)
EXCHANGE_OUTBOX_JOB_COLUMNS = (
    "id",
    "kind",
    "organization_id",
    "workspace_id",
    "actor_issuer",
    "actor_subject",
    "connection_id",
    "binding_id",
    "operation_id",
    "idempotency_key",
    "state",
    "attempts",
    "available_at",
    "claim_owner",
    "claim_fence",
    "lease_expires_at",
    "created_at",
    "updated_at",
    "document",
)
ACCOUNT_CENTER_UNKNOWN_PRESERVATION_SCHEMA = "cyrene-account-center-unknown-preservation-v1"
ACCOUNT_CENTER_UNKNOWN_PRESERVATION_MAX_BYTES = 256 * 1024
ACCOUNT_CENTER_UNKNOWN_PRESERVATION_MAX_AGE_SECONDS = 30 * 60
ACCOUNT_CENTER_STARTUP_RECOVERY_PREDICATES = frozenset(
    {
        "provider_profiles_active_refresh_claims",
        "provider_profiles_active_stale_refresh_claims",
        "accounts_refresh_pending",
        "accounts_active_refresh_claims",
        "provider_profiles_hf_reauth_lease_deletions",
        "accounts_reauth_lease_deletions",
        "accounts_pending_remote_revocation",
        "microsoft_entra_processing_sessions",
        "github_processing_link_sessions_without_blob",
        "github_interrupted_callback_revocations",
        "hf_device_starting_stale",
        "hf_device_active_poll_claims",
        "hf_device_poll_stale_foreign_claim",
        "accounts_interrupted_unlink_commands",
        "pending_idempotency",
    }
)
ACCOUNT_CENTER_LIVE_IDLE_COUNTS = frozenset(
    {
        "activeProviderCredentialLeases",
        "activeAccountCredentialLeases",
    }
)
ACCOUNT_CENTER_PROTECTED_PROFILE_PROVIDERS = frozenset(
    {"github", "huggingface", "azure", "gcp", "runpod"}
)
ACCOUNT_CENTER_MICROSOFT_PROFILE_PROVIDER = "microsoft_graph"
AUTOMATION_RUNTIME_SERVICE_SET = RUNTIME_SERVICE_SET | set(OPTIONAL_RUNTIME_SERVICE_ORDER)
# A hot memory update may target one already-running service from the active profile.
# It never adds a service or changes the mutable-stage target set.
MEMORY_UPDATE_TARGETS = AUTOMATION_RUNTIME_SERVICE_SET
MEMORY_UPDATE_MUTATION_MODE = "DOCKER_UPDATE_IN_PLACE_MEMORY_ONLY"
RECOVERED_PERSISTENCE_PRINCIPAL_CONFIG = "recovered-navigator-persistence-navigator-principal"
NAVIGATOR_PRINCIPAL_CONFIG_FILENAME = "navigator-principal-config.json"
NAVIGATOR_PRINCIPAL_CONFIG_TARGET = "/run/secrets/principal-config.json"
PROJECT_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}\Z")
FULL_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_ID_RE = runtime_update.IMAGE_ID_RE
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
SELECTION_PATH_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
DATABASE_PATH = "/var/lib/cyrene-navigator/persistence.db"
DATABASE_VOLUME_TARGET = "/var/lib/cyrene-navigator"
CODEBUDDY_HOME_TARGET = "/home/navigator/.codebuddy"
AUTOMATION_HEALTH_TARGET = "/run/cyrene/automation-health"
MEMORY_RESERVE_DEFAULT_MIB = 1024
SQLITE_ADDITIVE_SCHEMA_OBJECTS = {"table:work_credential_authorization_health"}
ACCOUNT_CENTER_DOWNGRADE_BLOCKED_ERROR = "account_center_schema_downgrade_unsupported"


def _exchange_readiness_required(
    phase: str, image_update_target: str | None, *, listener_repair: bool = False
) -> bool:
    """Require live Exchange verification for activation or a dependent image update.

    中文：连接器激活以及 Exchange/Cloud 镜像更新都必须现场验证连接器目录。
    """

    return (
        phase in {"issued-connector-activation", OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE}
        or image_update_target in IMAGE_UPDATE_READINESS_TARGETS
        or listener_repair
    )


def _runtime_service_shape(services: dict[str, Any], label: str) -> frozenset[str]:
    """Validate the fixed 14-service runtime with optional Graph/ADO additions."""

    names = frozenset(services)
    optional = names - RUNTIME_SERVICE_SET
    if not RUNTIME_SERVICE_SET <= names or optional not in {
        frozenset(),
        frozenset({"microsoft-graph"}),
        frozenset(OPTIONAL_RUNTIME_SERVICE_ORDER),
    }:
        suffix = (
            "exact active 14-service topology or supported Graph/ADO extension"
            if "memory update" in label
            else "exact 14-service topology or supported Graph/ADO extension"
        )
        raise StageTransitionError(f"{label} violates the {suffix}")
    return names


def _runtime_service_order_for(services: dict[str, Any]) -> tuple[str, ...]:
    """Order a validated runtime subset using the canonical service dependencies.

    中文：拓扑验证负责检查各阶段的完整服务集合；这里也要支持老阶段的局部合成夹具。
    """

    names = frozenset(services)
    unsupported = names - (RUNTIME_SERVICE_SET | set(OPTIONAL_RUNTIME_SERVICE_ORDER))
    if unsupported:
        raise StageTransitionError("runtime inventory contains an unsupported service")
    return (
        *[name for name in RUNTIME_SERVICE_ORDER if name in names],
        *(name for name in OPTIONAL_RUNTIME_SERVICE_ORDER if name in names),
    )


CLOUD_MANAGEMENT_TLS_SECRET_NAMES = frozenset(
    {
        "cloud-management-server-cert",
        "cloud-management-server-key",
        "cloud-management-health-client-cert",
        "cloud-management-health-client-key",
    }
)
SQLITE_CREDENTIAL_HEALTH_TABLE_SQL = """CREATE TABLE work_credential_authorization_health (
 organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, connection_id TEXT NOT NULL,
 provider TEXT NOT NULL, generation INTEGER NOT NULL, fingerprint TEXT NOT NULL,
 decision TEXT NOT NULL, checked_at TEXT, expires_at TEXT, reason_code TEXT,
 incident_id TEXT, incident_started_at INTEGER, last_notified_at INTEGER,
 reminder_count INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
 PRIMARY KEY(organization_id,workspace_id,connection_id))"""
EXCHANGE_READINESS_EXECUTABLE = (
    "/app/Cyrene-Services/Cyrene-Exchange/product/.venv/bin/cyrene-exchange"
)
EXCHANGE_SECRET_ENV_ENTRYPOINT = "/usr/local/bin/cyrene-secret-env-entrypoint"
EXCHANGE_INTEGRATION_DATABASE = "/data/integrations.sqlite3"
_EXCHANGE_ISSUED_STATE_PYTHON = r"""
import argparse
import json
import sys
from pathlib import Path

from cyrene_exchange_product.cli import _integration_connector_export_data
from cyrene_exchange_product.integrations.cli import build_production_services

database, issuer, subject, organization_id, workspace_id = sys.argv[1:]
arguments = argparse.Namespace(
    issuer=issuer,
    subject=subject,
    organization_id=organization_id,
    workspace_id=workspace_id,
)
services = build_production_services(Path(database))
try:
    exported = _integration_connector_export_data(
        services,
        arguments,
        include_profile_metadata=True,
        include_live_catalog=False,
    )
    result = {
        "schemaVersion": 1,
        "actor": {
            "issuer": issuer,
            "subject": subject,
            "organizationId": organization_id,
            "workspaceId": workspace_id,
        },
        **exported,
    }
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 16 * 1024 * 1024:
        raise SystemExit("issued connector metadata exceeds its bounded size")
    print(encoded)
finally:
    services.close()
"""
PROVIDER_DEFAULT_COMMANDS = {
    "huggingface": (
        "--entrypoint",
        "huggingface_provider.plugin:HuggingFaceProvider",
        "--capability",
        "tool.provider.v1",
        "--interface-version",
        "1",
        "--listen",
        "huggingface:7443",
        "--tls-cert-file",
        "/run/secrets/huggingface-server-cert",
        "--tls-key-file",
        "/run/secrets/huggingface-server-key",
        "--tls-client-ca-file",
        "/run/secrets/huggingface-client-ca",
    ),
    "cloud-management-phase2": (
        "--entrypoint",
        "cloud_management_provider.plugin:CloudManagementProvider",
        "--capability",
        "tool.provider.v1",
        "--interface-version",
        "1",
        "--listen",
        "cloud-management:7443",
        "--tls-cert-file",
        "/run/secrets/cloud-management-server-cert",
        "--tls-key-file",
        "/run/secrets/cloud-management-server-key",
        "--tls-client-ca-file",
        "/run/secrets/cloud-management-client-ca",
    ),
}
PROVIDER_WILDCARD_COMMANDS = {
    service: tuple(
        "0.0.0.0:7443" if index == command.index("--listen") + 1 else value
        for index, value in enumerate(command)
    )
    for service, command in PROVIDER_DEFAULT_COMMANDS.items()
}

_SQLITE_REPORT_PYTHON = r"""
import base64
import hashlib
import json
import math
import re
import sqlite3
import sys

def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'

def normalize_value(value):
    if isinstance(value, bytes):
        return {"$bytesBase64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": repr(value)}
    return value

def digest_rows(connection, table, columns, order_columns):
    if not columns:
        return 0, hashlib.sha256(b"").hexdigest()
    selected = ",".join(quote_identifier(name) for name in columns)
    ordered = list(dict.fromkeys(order_columns + columns))
    order = ",".join(quote_identifier(name) for name in ordered)
    query = "SELECT " + selected + " FROM " + quote_identifier(table) + " ORDER BY " + order
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(query):
        encoded = json.dumps(
            [normalize_value(value) for value in row],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.hexdigest()

def check_constraints(sql):
    if not isinstance(sql, str):
        return []
    result = []
    offset = 0
    pattern = re.compile(r"\bCHECK\s*\(", re.IGNORECASE)
    while True:
        match = pattern.search(sql, offset)
        if match is None:
            break
        start = match.end()
        depth = 1
        quote = None
        index = start
        while index < len(sql) and depth:
            char = sql[index]
            if quote is not None:
                if char == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
            elif char in "'\"`":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        if depth:
            raise ValueError("unbalanced CHECK expression")
        result.append(" ".join(sql[start:index - 1].split()).lower())
        offset = index
    return sorted(result)

def report_database(database_path, comparison_contract=None):
    connection = sqlite3.connect("file:" + database_path + "?mode=ro", uri=True)
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    schema_rows = connection.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    schema_objects = [
        {"type": row[0], "name": row[1], "sql": row[2]} for row in schema_rows
    ]
    tables = {}
    table_names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for table in table_names:
        table_info = connection.execute(
            "PRAGMA table_xinfo(" + quote_identifier(table) + ")"
        ).fetchall()
        columns = [
            {
                "name": row[1],
                "type": row[2] or "",
                "notNull": bool(row[3]),
                "default": row[4],
                "primaryKeyOrdinal": int(row[5]),
                "hidden": int(row[6]) if len(row) > 6 else 0,
            }
            for row in table_info
        ]
        data_columns = [row["name"] for row in columns if row["hidden"] != 1]
        primary_key = [
            row["name"]
            for row in sorted(columns, key=lambda item: item["primaryKeyOrdinal"])
            if row["primaryKeyOrdinal"] > 0
        ]
        foreign_keys = {}
        for row in connection.execute(
            "PRAGMA foreign_key_list(" + quote_identifier(table) + ")"
        ).fetchall():
            group = foreign_keys.setdefault(
                int(row[0]),
                {
                    "table": row[2],
                    "onUpdate": row[5],
                    "onDelete": row[6],
                    "match": row[7],
                    "columns": [],
                },
            )
            group["columns"].append(
                {"sequence": int(row[1]), "from": row[3], "to": row[4]}
            )
        foreign_key_records = []
        for group in foreign_keys.values():
            group["columns"].sort(key=lambda item: item["sequence"])
            for column in group["columns"]:
                column.pop("sequence")
            foreign_key_records.append(group)
        foreign_key_records.sort(key=lambda item: json.dumps(item, sort_keys=True))
        unique_indexes = []
        for index_row in connection.execute(
            "PRAGMA index_list(" + quote_identifier(table) + ")"
        ).fetchall():
            index_name = index_row[1]
            origin = index_row[3] if len(index_row) > 3 else "c"
            if origin == "pk":
                continue
            index_columns = [
                {
                    "name": row[2],
                    "descending": bool(row[3]),
                    "collation": row[4],
                    "key": bool(row[5]),
                }
                for row in connection.execute(
                    "PRAGMA index_xinfo(" + quote_identifier(index_name) + ")"
                ).fetchall()
                if bool(row[5])
            ]
            unique_indexes.append(
                {
                    "name": index_name,
                    "unique": bool(index_row[2]),
                    "origin": origin,
                    "partial": bool(index_row[4]) if len(index_row) > 4 else False,
                    "columns": index_columns,
                }
            )
        unique_indexes.sort(key=lambda item: item["name"])
        create_sql = next(
            (row[2] for row in schema_rows if row[0] == "table" and row[1] == table),
            None,
        )
        row_count, rows_digest = digest_rows(
            connection, table, data_columns, primary_key or data_columns
        )
        record = {
            "columns": columns,
            "createSql": create_sql,
            "primaryKeyColumns": primary_key,
            "foreignKeys": foreign_key_records,
            "checkConstraints": check_constraints(create_sql),
            "uniqueIndexes": unique_indexes,
            "rowCount": row_count,
            "rowsDigest": rows_digest,
        }
        if any(column["name"] == "organization_id" for column in columns):
            scope = connection.execute(
                "SELECT COUNT(*) FROM " + quote_identifier(table) +
                " WHERE organization_id = ''"
            ).fetchone()[0]
            record["emptyOrganizationIdRows"] = int(scope)
        tables[table] = record

    comparison_contract = comparison_contract or {}
    for table, contract in comparison_contract.items():
        if table not in tables:
            raise ValueError("a previously present table disappeared")
        current_columns = {column["name"] for column in tables[table]["columns"]}
        columns = contract.get("columns")
        order_columns = contract.get("primaryKeyColumns")
        if (
            not isinstance(columns, list)
            or not isinstance(order_columns, list)
            or not set(columns) <= current_columns
            or not set(order_columns) <= current_columns
        ):
            raise ValueError("previous table columns are not available for reconciliation")
        count, digest = digest_rows(connection, table, columns, order_columns or columns)
        tables[table]["preservedRowsCount"] = count
        tables[table]["preservedRowsDigest"] = digest

    connection.close()
    schema_digest = hashlib.sha256(
        json.dumps(
            schema_objects,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "integrity": integrity,
        "userVersion": version,
        "schemaSha256": schema_digest,
        "objects": [row[0] + ":" + row[1] for row in schema_rows],
        "schemaObjects": schema_objects,
        "tables": tables,
    }

def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
"""


class StageTransitionError(ValueError):
    """A staged runtime transition is malformed, unsafe, or not yet recoverable."""


def _utc_now() -> str:
    """Return the canonical UTC timestamp used in private transaction records."""

    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> bytes:
    """Serialize JSON deterministically for the immutable plan and selection digests."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _sha256_bytes(value: bytes) -> str:
    """Hash one byte string without returning its contents."""

    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    """Hash one regular non-symlink plan input without serializing its contents."""

    try:
        metadata = path.lstat()
    except OSError as error:
        raise StageTransitionError(f"stage input is unavailable: {path.name}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise StageTransitionError(f"stage input is not a regular file: {path.name}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise StageTransitionError(f"stage input could not be read: {path.name}") from error
    return digest.hexdigest()


def _file_record(path: Path, label: str) -> dict[str, str]:
    """Bind a single external input to its normalized path and SHA-256."""

    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise StageTransitionError(f"{label} is unavailable") from error
    return {"path": str(resolved), "sha256": _sha256_file(resolved)}


def _operator_private_file_record(path: Path, label: str) -> dict[str, str]:
    """Bind one owner-only operator evidence file without allowing symlinks or hard links."""

    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise StageTransitionError(f"{label} is unavailable") from error
    if (
        not preflight._private_artifact(path)
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size > ACCOUNT_CENTER_UNKNOWN_PRESERVATION_MAX_BYTES
        or str(resolved) != str(path)
    ):
        raise StageTransitionError(f"{label} must be a bounded owner-only single-link artifact")
    return {"path": str(resolved), "sha256": _sha256_file(resolved)}


def _verify_operator_private_file_record(record: Any, label: str) -> None:
    """Recheck the private artifact mode, owner, link count, path and digest."""

    if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
        raise StageTransitionError(f"{label} record is malformed")
    path_text = record.get("path")
    digest = record.get("sha256")
    if (
        not isinstance(path_text, str)
        or not isinstance(digest, str)
        or SHA256_RE.fullmatch(digest) is None
    ):
        raise StageTransitionError(f"{label} record is malformed")
    current = _operator_private_file_record(Path(path_text), label)
    if current != record:
        raise StageTransitionError(f"{label} changed after planning")


def _file_records(paths: list[Path], label: str) -> list[dict[str, str]]:
    """Bind an ordered set of Compose or environment inputs to their digests."""

    if not paths:
        raise StageTransitionError(f"at least one {label} is required")
    return [_file_record(path, label) for path in paths]


def _directory_manifest(path: Path, label: str) -> list[dict[str, Any]]:
    """Fingerprint every regular file in one private directory without storing contents."""

    resolved = _validate_private_directory(
        path,
        label,
        expected_uid=10001,
    )
    records: list[dict[str, Any]] = []
    for child in sorted(resolved.rglob("*")):
        try:
            metadata = child.lstat()
        except OSError as error:
            raise StageTransitionError(f"{label} directory changed while inventorying") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise StageTransitionError(f"{label} directory contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            if metadata.st_uid != 10001 or metadata.st_mode & 0o077:
                raise StageTransitionError(f"{label} subdirectory must be private UID 10001")
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 10001
            or metadata.st_mode & 0o077
        ):
            raise StageTransitionError(f"{label} contains a non-private regular file")
        records.append(
            {
                "relativePath": str(child.relative_to(resolved)),
                "sizeBytes": metadata.st_size,
                "mode": stat.S_IMODE(metadata.st_mode),
                "sha256": _sha256_file(child),
            }
        )
    return records


def _validate_automation_health_source(path: Path, canonical_source: str) -> Any:
    """Validate the fixed owner/group/mode contract for mutable health snapshots."""

    try:
        resolved = path.resolve(strict=True)
        link_metadata = path.lstat()
        metadata = path.stat()
    except OSError as error:
        raise StageTransitionError("automation health mount source is unavailable") from error
    if (
        not path.is_absolute()
        or str(path) != canonical_source
        or str(resolved) != canonical_source
        or stat.S_ISLNK(link_metadata.st_mode)
        or not stat.S_ISDIR(link_metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 10001
        or stat.S_IMODE(metadata.st_mode) != 0o750
        or not preflight._private_artifact(path)
    ):
        raise StageTransitionError(
            "automation health mount must be a canonical root:10001 mode 0750 directory"
        )
    return metadata


def _secret_manifest(compose: dict[str, Any], label: str) -> list[dict[str, Any]]:
    """Fingerprint only explicit Compose secret sources, never walk the whole secrets directory."""

    secrets = compose.get("secrets", {})
    if not isinstance(secrets, dict):
        raise StageTransitionError(f"{label} Compose secret inventory is malformed")
    records: list[dict[str, Any]] = []
    for name, row in sorted(secrets.items()):
        file_text = row.get("file") if isinstance(row, dict) else None
        if not isinstance(name, str) or not isinstance(file_text, str):
            raise StageTransitionError(f"{label} Compose secret source is malformed")
        path = Path(file_text)
        record = _file_record(path, f"{label} secret {name}")
        metadata = path.stat()
        records.append(
            {
                "name": name,
                **record,
                "sizeBytes": metadata.st_size,
                "mode": stat.S_IMODE(metadata.st_mode),
                "uid": metadata.st_uid,
            }
        )
    return records


def _verify_secret_manifest(records: Any, label: str) -> None:
    """Recheck exact Compose secret path, content digest, owner, mode, and size."""

    if not isinstance(records, list):
        raise StageTransitionError(f"{label} secret fingerprint is malformed")
    for row in records:
        if not isinstance(row, dict) or set(row) != {
            "name",
            "path",
            "sha256",
            "sizeBytes",
            "mode",
            "uid",
        }:
            raise StageTransitionError(f"{label} secret fingerprint is malformed")
        path = Path(row["path"])
        try:
            metadata = path.stat()
        except OSError as error:
            raise StageTransitionError(
                f"{label} secret {row.get('name')} disappeared after planning"
            ) from error
        if (
            str(path.resolve(strict=True)) != row["path"]
            or metadata.st_size != row["sizeBytes"]
            or stat.S_IMODE(metadata.st_mode) != row["mode"]
            or metadata.st_uid != row["uid"]
            or _sha256_file(path) != row["sha256"]
        ):
            raise StageTransitionError(f"{label} secret {row.get('name')} changed after planning")


def _read_json(path: Path, label: str, *, maximum_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    """Read one bounded duplicate-key-free JSON object without echoing values."""

    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise StageTransitionError(f"{label} is not a bounded regular file")
        raw = path.read_bytes()
        if len(raw) > maximum_bytes:
            raise StageTransitionError(f"{label} exceeds the input size limit")
        value = json.loads(
            raw,
            object_pairs_hook=preflight._unique_json_object,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        if isinstance(error, StageTransitionError):
            raise
        raise StageTransitionError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise StageTransitionError(f"{label} must be a JSON object")
    return value


def _runtime_service_inventory(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Select active long-running services from the exact rendered profile set."""

    services = compose.get("services")
    if not isinstance(services, dict):
        raise StageTransitionError("rendered Compose has no service inventory")
    active_profiles = {"public-ingress", "cloud-phase2", "runpod"}
    result: dict[str, dict[str, Any]] = {}
    for name, service in services.items():
        if not isinstance(name, str) or not isinstance(service, dict):
            raise StageTransitionError("rendered Compose service inventory is malformed")
        if service.get("restart") == "no":
            continue
        profiles = service.get("profiles", [])
        if not isinstance(profiles, list) or any(not isinstance(item, str) for item in profiles):
            raise StageTransitionError(f"Compose profiles are malformed for {name}")
        if profiles and not active_profiles.intersection(profiles):
            continue
        if name == "cloud-management-phase2" and not set(profiles).intersection(
            {"cloud-phase2", "runpod"}
        ):
            raise StageTransitionError(
                "the staged Cloud Management service must use cloud-phase2 or its optional RunPod profile"
            )
        result[name] = service
    if "navigator" not in result or "navigator-persistence" not in result:
        raise StageTransitionError("Navigator and its Persistence service are required")
    return result


def _lock_image_ids(lock: dict[str, Any]) -> dict[str, str]:
    """Reuse the canonical runtime-update image-lock validator."""

    try:
        return runtime_update._locked_image_ids(lock)
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError("active or candidate image lock is invalid") from error


def _office_oauth_setup_bridge_enabled(compose: dict[str, Any]) -> bool:
    """Recognize only the private Office operator mount and its fixed loopback port."""

    services = compose.get("services", {})
    exchange = services.get("exchange", {}) if isinstance(services, dict) else {}
    volumes = exchange.get("volumes", []) if isinstance(exchange, dict) else []
    mounted = isinstance(volumes, list) and any(
        isinstance(row, dict) and row.get("target") == "/run/config/office-operator.json"
        for row in volumes
    )
    phase = preflight.OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE if mounted else None
    ports = exchange.get("ports", []) if isinstance(exchange, dict) else []
    try:
        preflight._validate_office_oauth_setup_mount(compose, phase)
        preflight._validate_office_oauth_loopback_ports(ports, phase)
    except preflight.PreflightError as error:
        raise StageTransitionError(
            "Exchange Office OAuth published bridge is not the exact private loopback setup"
        ) from error
    return mounted


def _validate_office_oauth_internal_networks(networks: Any, *, docker_prefix: list[str]) -> None:
    """Require every attached Exchange network to be the exact inspected internal network."""

    if not isinstance(networks, dict) or not networks:
        raise StageTransitionError("Exchange Office OAuth network inventory is empty or malformed")
    network_ids: dict[str, str] = {}
    for name, attachment in networks.items():
        network_id = attachment.get("NetworkID") if isinstance(attachment, dict) else None
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(network_id, str)
            or FULL_CONTAINER_ID_RE.fullmatch(network_id) is None
        ):
            raise StageTransitionError(
                "Exchange Office OAuth attached network identity is malformed"
            )
        network_ids[name] = network_id
    try:
        inspected = runtime_update._decode_json_output(
            runtime_update._checked_stdout(
                [*docker_prefix, "network", "inspect", *sorted(network_ids)],
                label="Exchange Office OAuth internal network inspection",
            ),
            "Exchange Office OAuth internal network inspection",
        )
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError("Exchange Office OAuth network inspection failed") from error
    if not isinstance(inspected, list) or len(inspected) != len(network_ids):
        raise StageTransitionError("Exchange Office OAuth network inspection is incomplete")
    observed: dict[str, str] = {}
    for row in inspected:
        name = row.get("Name") if isinstance(row, dict) else None
        network_id = row.get("Id") if isinstance(row, dict) else None
        if (
            not isinstance(name, str)
            or name in observed
            or network_ids.get(name) != network_id
            or row.get("Internal") is not True
        ):
            raise StageTransitionError(
                "Exchange Office OAuth attached network is not the exact internal network"
            )
        observed[name] = network_id
    if set(observed) != set(network_ids):
        raise StageTransitionError("Exchange Office OAuth network inspection names changed")


def _service_image(compose: dict[str, Any], image_ids: dict[str, str], name: str) -> dict[str, str]:
    """Resolve one Compose service to its exact locked immutable image reference and ID."""

    service = compose.get("services", {}).get(name)
    image_ref = service.get("image") if isinstance(service, dict) else None
    image_id = image_ids.get(image_ref) if isinstance(image_ref, str) else None
    if (
        not isinstance(image_ref, str)
        or not isinstance(image_id, str)
        or not IMAGE_ID_RE.fullmatch(image_id)
    ):
        raise StageTransitionError(f"{name} image does not resolve to an exact locked image")
    return {"imageRef": image_ref, "imageId": image_id}


def _exchange_cloud_runner_compose_value(compose: dict[str, Any]) -> str:
    """Require the Exchange background refresh runner to be explicitly disabled.

    中文：Compose 必须显式配置 runner=false，不能依赖默认值。
    """

    services = compose.get("services", {})
    exchange = services.get("exchange") if isinstance(services, dict) else None
    environment = exchange.get("environment", {}) if isinstance(exchange, dict) else None
    value = environment.get(EXCHANGE_CLOUD_RUNNER_ENV) if isinstance(environment, dict) else None
    if value != "false":
        raise StageTransitionError(
            "Cloud Management image update requires Exchange AuthorizationCloudRunner=false"
        )
    return value


def _exchange_integration_database_container_path(
    *, container_id: str, docker_prefix: list[str]
) -> str:
    """Require the exact Exchange container command to use its separately mounted DB.

    中文：只接受镜像固定的产品数据库路径，拒绝不明 CLI override。
    """

    if FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError("Exchange cloud runner container identity is malformed")
    try:
        output = runtime_update._checked_stdout(
            [
                *docker_prefix,
                "inspect",
                "--format",
                "{{json .Config.Cmd}}",
                container_id,
            ],
            label="Exchange integration database command readback",
        )
        command = runtime_update._decode_json_output(
            output, "Exchange integration database command readback"
        )
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "Exchange integration database path could not be verified from its exact container"
        ) from error
    if not isinstance(command, list) or any(not isinstance(value, str) for value in command):
        raise StageTransitionError("Exchange container command readback is malformed")
    database_paths = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--integration-database"
    ]
    if database_paths != [EXCHANGE_INTEGRATION_DATABASE]:
        raise StageTransitionError(
            "Exchange container is not pinned to the canonical integration database path"
        )
    return database_paths[0]


def _exchange_cloud_runner_container_value(*, container_id: str, docker_prefix: list[str]) -> str:
    """Read only the runner flag from Docker metadata without retaining other env values.

    中文：只返回精确 runner 标记，不持久化或输出其余容器环境变量。
    """

    if FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError("Exchange cloud runner container identity is malformed")
    try:
        output = runtime_update._checked_stdout(
            [
                *docker_prefix,
                "inspect",
                "--format",
                "{{json .Config.Env}}",
                container_id,
            ],
            label="Exchange cloud runner environment readback",
        )
        environment = runtime_update._decode_json_output(
            output, "Exchange cloud runner environment readback"
        )
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "Exchange cloud runner state could not be verified from its exact container"
        ) from error
    if not isinstance(environment, list) or any(not isinstance(item, str) for item in environment):
        raise StageTransitionError("Exchange cloud runner environment readback is malformed")
    matching = [
        item.partition("=")[2]
        for item in environment
        if item.partition("=")[0] == EXCHANGE_CLOUD_RUNNER_ENV
    ]
    if matching != ["false"]:
        raise StageTransitionError(
            "Cloud Management image update requires the live Exchange runner to be explicitly disabled"
        )
    return "false"


def _exchange_cloud_runner_idle_counts(
    *, container_id: str, docker_prefix: list[str]
) -> dict[str, int]:
    """Read only fixed outbox/monitor counts from the exact Exchange SQLite file.

    中文：只查非终态作业与未过期 lease 的计数，不读取任务文档或凭据。
    """

    if FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError("Exchange cloud runner container identity is malformed")
    python = str(Path(EXCHANGE_READINESS_EXECUTABLE).with_name("python"))
    script = (
        "import json,sqlite3,sys\n"
        "database_path,now_utc=sys.argv[1:3]\n"
        "connection=sqlite3.connect('file:'+database_path+'?mode=ro',uri=True,timeout=5)\n"
        "try:\n"
        " connection.execute('PRAGMA query_only=ON')\n"
        " connection.execute('BEGIN')\n"
        " cloud_nonterminal=connection.execute(\"SELECT COUNT(*) FROM cloud_refresh_outbox WHERE state IN ('QUEUED','RUNNING')\").fetchone()[0]\n"
        " cloud_active_leases=connection.execute(\"SELECT COUNT(*) FROM cloud_refresh_outbox WHERE state IN ('QUEUED','RUNNING') AND claim_owner IS NOT NULL AND lease_expires_at>?\",(now_utc,)).fetchone()[0]\n"
        ' monitor_active_leases=connection.execute("SELECT COUNT(*) FROM authorization_monitor_state WHERE claim_owner IS NOT NULL AND lease_expires_at>?",(now_utc,)).fetchone()[0]\n'
        " email_nonterminal=connection.execute(\"SELECT COUNT(*) FROM email_notification_outbox WHERE state IN ('READY','CLAIMED','SENDING')\").fetchone()[0]\n"
        " result={'cloudRefreshNonterminalCount':cloud_nonterminal,'cloudRefreshActiveLeaseCount':cloud_active_leases,'authorizationMonitorActiveLeaseCount':monitor_active_leases,'emailNotificationNonterminalCount':email_nonterminal}\n"
        " print(json.dumps(result,sort_keys=True,separators=(',',':')))\n"
        "finally:\n"
        " connection.close()\n"
    )
    try:
        output = runtime_update._checked_stdout(
            [
                *docker_prefix,
                "exec",
                "--user",
                "10001:10001",
                container_id,
                python,
                "-c",
                script,
                EXCHANGE_INTEGRATION_DATABASE,
                datetime.now(UTC).isoformat(),
            ],
            label="Exchange cloud worker idle readback",
            timeout_seconds=15,
        )
        report = runtime_update._decode_json_output(output, "Exchange cloud worker idle report")
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "Exchange cloud refresh and monitor queues could not be verified read-only"
        ) from error
    expected_fields = {
        "cloudRefreshNonterminalCount",
        "cloudRefreshActiveLeaseCount",
        "authorizationMonitorActiveLeaseCount",
        "emailNotificationNonterminalCount",
    }
    if (
        not isinstance(report, dict)
        or set(report) != expected_fields
        or any(
            isinstance(report.get(field), bool)
            or not isinstance(report.get(field), int)
            or report[field] < 0
            for field in expected_fields
        )
    ):
        raise StageTransitionError("Exchange cloud worker idle report is malformed")
    if any(report[field] != 0 for field in expected_fields):
        raise StageTransitionError(
            "Exchange has queued/running cloud, authorization-monitor, or email work"
        )
    return report


def _account_center_host_gh_cli_process_count() -> int:
    """Count host GitHub CLI processes without reading or returning their argv."""

    process_root = Path("/proc")
    try:
        process_entries = list(process_root.iterdir())
    except OSError as error:
        raise StageTransitionError(
            "host GitHub CLI process state could not be verified read-only"
        ) from error
    count = 0
    for process_entry in process_entries:
        if not process_entry.name.isdecimal():
            continue
        try:
            process_name = (process_entry / "comm").read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise StageTransitionError(
                "host GitHub CLI process state could not be verified read-only"
            ) from error
        count += int(process_name == "gh")
    return count


def _verify_provider_monitor_quiescence(
    *,
    plan: dict[str, Any],
    candidate_compose: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
    checkpoint: str = "before",
) -> dict[str, Any]:
    """Require the selected connector monitors and calls to stay clear through startup.

    References come from the just-verified native Exchange snapshot and must join the frozen
    binding set before they are used in read-only SQL. Cloud Management also requires its
    durable refresh outbox to be empty. 中文：按实际签发连接只读检查监视器与调用窗口。
    """

    if checkpoint not in {"before", "after"}:
        raise StageTransitionError("connector monitor checkpoint is unsupported")
    target = plan.get("imageUpdateTarget")
    preserved_unknown = None
    if target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET:
        image_state = plan.get("accountCenterImageUpdateState")
        preserved_unknown = (
            image_state.get("preservedUnknown") if isinstance(image_state, dict) else None
        )
        if preserved_unknown is not None:
            _validate_account_center_preserved_unknown_state(preserved_unknown)
    if target == HUGGINGFACE_IMAGE_UPDATE_TARGET:
        target_journal_key = "huggingfaceImageUpdate"
        required_providers = [HUGGINGFACE_IMAGE_UPDATE_TARGET]
        include_cloud_refresh = False
    elif (
        target == GITHUB_ISSUES_IMAGE_UPDATE_TARGET
        and plan.get("phase") == "issued-connector-activation"
    ):
        target_journal_key = "githubIssuesImageUpdate"
        required_providers = ["github"]
        include_cloud_refresh = False
    elif target == CLOUD_IMAGE_UPDATE_TARGET and plan.get("phase") == "issued-connector-activation":
        target_journal_key = "cloudImageUpdate"
        expected_scope_digests = plan.get("candidatePreflight", {}).get(
            "expectedProviderScopeDigests"
        )
        if not isinstance(expected_scope_digests, dict):
            raise StageTransitionError("candidate Cloud monitor provider scope is malformed")
        required_providers = sorted(set(expected_scope_digests) & CLOUD_MONITOR_PROVIDER_IDS)
        if not {"azure", "gcp"} <= set(required_providers):
            raise StageTransitionError(
                "Cloud image update requires its issued Azure and GCP bindings"
            )
        include_cloud_refresh = True
    elif (
        target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
        and plan.get("phase") in ACCOUNT_CENTER_IMAGE_UPDATE_PHASES
    ):
        target_journal_key = "accountCenterImageUpdate"
        expected_scope_digests = plan.get("candidatePreflight", {}).get(
            "expectedProviderScopeDigests"
        )
        if not isinstance(expected_scope_digests, dict):
            raise StageTransitionError("candidate Account Center provider scopes are malformed")
        if "microsoft_graph" in expected_scope_digests:
            raise StageTransitionError(
                "Account Center monitor scopes must exclude the unissued Microsoft Graph profile"
            )
        required_providers = sorted(
            (set(expected_scope_digests) & {"huggingface", "azure", "gcp", "runpod"}) | {"github"}
        )
        if not {"github", "huggingface", "azure", "gcp"} <= set(required_providers):
            raise StageTransitionError(
                "Account Center image update requires the existing GitHub/HF/Azure/GCP monitor bindings"
            )
        include_cloud_refresh = False
    else:
        raise StageTransitionError(
            "connector monitor gate is restricted to issued Account Center/GitHub/HF/Cloud image updates"
        )

    readiness = journal.get("exchangeReadiness")
    receipt = readiness.get("receipt") if isinstance(readiness, dict) else None
    if target == HUGGINGFACE_IMAGE_UPDATE_TARGET:
        connection_refs = (
            {HUGGINGFACE_IMAGE_UPDATE_TARGET: receipt.get("huggingfaceConnectionRef")}
            if isinstance(receipt, dict)
            else {}
        )
    elif target == GITHUB_ISSUES_IMAGE_UPDATE_TARGET:
        github_refs = receipt.get("githubConnectionRefs") if isinstance(receipt, dict) else None
        connection_refs = {"github": github_refs}
    elif target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET:
        refs = receipt.get("connectionRefByProvider") if isinstance(receipt, dict) else None
        connection_refs = (
            {provider: refs.get(provider) for provider in required_providers}
            if isinstance(refs, dict)
            else {}
        )
    else:
        refs = receipt.get("connectionRefByProvider") if isinstance(receipt, dict) else None
        connection_refs = refs if isinstance(refs, dict) else {}
    if set(connection_refs) != set(required_providers):
        raise StageTransitionError(
            "live Exchange readiness did not provide exactly the configured monitor connections"
        )
    canonical_refs: dict[str, list[str]] = {}
    for provider, provider_refs in connection_refs.items():
        refs = provider_refs if isinstance(provider_refs, list) else [provider_refs]
        if (
            not refs
            or (provider == "github" and len(refs) != 10)
            or any(not isinstance(connection_ref, str) for connection_ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise StageTransitionError(
                f"live Exchange readiness did not provide the exact {provider} connection refs"
            )
        canonical_provider_refs: list[str] = []
        for connection_ref in refs:
            try:
                canonical = str(uuid.UUID(connection_ref))
            except (ValueError, TypeError, AttributeError) as error:
                raise StageTransitionError(
                    f"live Exchange readiness did not provide a canonical {provider} connection reference"
                ) from error
            if canonical != connection_ref:
                raise StageTransitionError(f"live {provider} connection reference is not canonical")
            canonical_provider_refs.append(canonical)
        canonical_refs[provider] = sorted(canonical_provider_refs)

    if preserved_unknown is not None and (
        canonical_refs.get("github", []).count(preserved_unknown["connectionRef"]) != 1
    ):
        raise StageTransitionError(
            "sealed GitHub UNKNOWN row does not belong to the exact live monitor binding set"
        )

    bindings = _candidate_config_json(candidate_compose, "connector-bindings").get("bindings")
    rows = bindings if isinstance(bindings, list) else []
    for provider, provider_refs in canonical_refs.items():
        selected = [
            row for row in rows if isinstance(row, dict) and row.get("providerId") == provider
        ]
        selected_refs = [
            row.get("connectionRef")
            for row in selected
            if isinstance(row.get("connectionRef"), str)
        ]
        if (
            len(selected) != len(provider_refs)
            or len(selected_refs) != len(selected)
            or set(selected_refs) != set(provider_refs)
        ):
            if len(provider_refs) == 1:
                raise StageTransitionError(
                    f"live {provider} connection does not match its single frozen candidate binding"
                )
            raise StageTransitionError(
                f"live {provider} connection refs do not match the frozen candidate bindings"
            )

    exchange = plan.get("activeServices", {}).get("exchange")
    if not isinstance(exchange, dict):
        raise StageTransitionError("active Exchange service identity is unavailable")
    container_id = exchange.get("containerId")
    if not isinstance(container_id, str) or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError("active Exchange container identity is malformed")
    actor = preflight._connector_bootstrap_actor(candidate_compose)
    if any(not isinstance(value, str) or not value for value in actor.values()):
        raise StageTransitionError("candidate Exchange actor is incomplete for the monitor gate")
    active_gh_process_count_by_service: dict[str, int] = {}
    active_host_gh_process_count: int | None = None
    if target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET:
        active_services = plan.get("activeServices")
        if (
            not isinstance(active_services, dict)
            or set(active_services) != ACCOUNT_CENTER_IMAGE_UPDATE_SERVICE_SET
        ):
            raise StageTransitionError(
                "Account Center process guard requires the exact 15-service inventory"
            )
        process_container_by_service = {
            service_name: service.get("containerId")
            for service_name, service in active_services.items()
            if isinstance(service, dict)
        }
        if len(process_container_by_service) != len(ACCOUNT_CENTER_IMAGE_UPDATE_SERVICE_SET):
            raise StageTransitionError("Account Center process service identity map is malformed")
        if checkpoint == "after":
            progress = journal.get("targetProgress")
            target_progress = (
                progress.get(ACCOUNT_CENTER_IMAGE_UPDATE_TARGET)
                if isinstance(progress, dict)
                else None
            )
            replacement_id = (
                target_progress.get("candidateContainerId")
                if isinstance(target_progress, dict)
                else None
            )
            if (
                not isinstance(target_progress, dict)
                or target_progress.get("phase") != "APPLIED"
                or not isinstance(replacement_id, str)
                or FULL_CONTAINER_ID_RE.fullmatch(replacement_id) is None
            ):
                raise StageTransitionError(
                    "post-update GitHub CLI guard lacks the applied Account Center container identity"
                )
            process_container_by_service[ACCOUNT_CENTER_IMAGE_UPDATE_TARGET] = replacement_id
        for service_name in sorted(ACCOUNT_CENTER_IMAGE_UPDATE_SERVICE_SET):
            process_container_id = process_container_by_service.get(service_name)
            if (
                not isinstance(process_container_id, str)
                or FULL_CONTAINER_ID_RE.fullmatch(process_container_id) is None
            ):
                raise StageTransitionError(
                    f"active {service_name} service identity is malformed for the GitHub CLI guard"
                )
            try:
                output = runtime_update._checked_stdout(
                    [*docker_prefix, "top", process_container_id, "-eo", "pid,comm"],
                    label=f"{service_name} active GitHub CLI process readback",
                    timeout_seconds=15,
                )
            except (
                OSError,
                runtime_update.RuntimeUpdateError,
                subprocess.SubprocessError,
            ) as error:
                raise StageTransitionError(
                    f"{service_name} GitHub CLI process state could not be verified read-only"
                ) from error
            if len(output.encode("utf-8")) > 64 * 1024:
                raise StageTransitionError(
                    f"{service_name} process inventory exceeds its bounded size"
                )
            process_rows = [line.split() for line in output.splitlines() if line.strip()]
            if not process_rows or [value.upper() for value in process_rows[0]] != [
                "PID",
                "COMMAND",
            ]:
                raise StageTransitionError(f"{service_name} process inventory header is malformed")
            if any(
                len(row) != 2 or not row[0].isdecimal() or not row[1] for row in process_rows[1:]
            ):
                raise StageTransitionError(f"{service_name} process inventory rows are malformed")
            active_gh_process_count_by_service[service_name] = sum(
                row[1].casefold() in {"gh", "gh.exe"} for row in process_rows[1:]
            )
            if active_gh_process_count_by_service[service_name] != 0:
                raise StageTransitionError(
                    "an active GitHub CLI process could replay a provider operation"
                )
        active_host_gh_process_count = _account_center_host_gh_cli_process_count()
        if active_host_gh_process_count != 0:
            raise StageTransitionError(
                "an active host GitHub CLI process could issue a provider operation"
            )
    timeout_seconds = plan.get("policy", {}).get("healthTimeoutSeconds")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 30 <= timeout_seconds <= 600
    ):
        raise StageTransitionError("connector image update health timeout is malformed")
    required_window_seconds = timeout_seconds + 60
    python = str(Path(EXCHANGE_READINESS_EXECUTABLE).with_name("python"))
    selected_connections = [
        {"providerId": provider, "connectionRef": connection_ref}
        for provider in required_providers
        for connection_ref in canonical_refs[provider]
    ]
    expected_monitor_count = sum(len(canonical_refs[provider]) for provider in required_providers)
    selected_connections_json = json.dumps(
        selected_connections, sort_keys=True, separators=(",", ":")
    )
    preservation_json = json.dumps(
        None
        if preserved_unknown is None
        else {
            "connectionRef": preserved_unknown["connectionRef"],
            "rowSha256": preserved_unknown["invocationRowSha256"],
            "requestIdSha256": preserved_unknown["requestIdSha256"],
            "operationIdSha256": preserved_unknown["operationIdSha256"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    invocation_columns_json = json.dumps(
        list(ACCOUNT_CENTER_UNKNOWN_INVOCATION_COLUMNS),
        separators=(",", ":"),
    )
    check_outbox_required = target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
    outbox_columns_json = json.dumps(list(EXCHANGE_OUTBOX_JOB_COLUMNS), separators=(",", ":"))
    script = (
        "import hashlib,json,sqlite3,sys\n"
        "from datetime import datetime,timedelta,timezone\n"
        "database_path,selected_text,issuer,subject,organization_id,workspace_id,window_text,cloud_text,preservation_text,columns_text,outbox_text,outbox_columns_text=sys.argv[1:13]\n"
        "selected=json.loads(selected_text)\n"
        "window=int(window_text)\n"
        "cloud_refresh_required=cloud_text=='true'\n"
        "preservation=json.loads(preservation_text)\n"
        "invocation_columns=json.loads(columns_text)\n"
        "outbox_idle_required=outbox_text=='true'\n"
        "outbox_columns=json.loads(outbox_columns_text)\n"
        "now=datetime.now(timezone.utc)\n"
        "def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()\n"
        "def cell(value):\n"
        " if isinstance(value,bytes): return {'sqliteType':'blob','sha256':hashlib.sha256(value).hexdigest(),'bytes':len(value)}\n"
        " return {'sqliteType':type(value).__name__,'value':value}\n"
        "def utc(value,label):\n"
        " if not isinstance(value,str) or not value: raise RuntimeError(label+' is missing')\n"
        " parsed=datetime.fromisoformat(value[:-1]+'+00:00' if value.endswith('Z') else value)\n"
        " if parsed.tzinfo is None or parsed.utcoffset()!=timedelta(0): raise RuntimeError(label+' is not UTC')\n"
        " return parsed.astimezone(timezone.utc)\n"
        "db=sqlite3.connect('file:'+database_path+'?mode=ro',uri=True,timeout=5)\n"
        "try:\n"
        " db.execute('PRAGMA query_only=ON')\n"
        " db.execute('BEGIN')\n"
        " if not isinstance(selected,list) or not selected: raise RuntimeError('selected monitor connections are malformed')\n"
        " stored_provider={'github':'github','huggingface':'hf','azure':'azure','gcp':'gcp','runpod':'runpod'}\n"
        " active_leases=0\n"
        " inflight_calls=0\n"
        " active_outbox_jobs=0\n"
        " active_outbox_leases=0\n"
        " preserved_unknown_count=0\n"
        " preserved_unknown_sha=None\n"
        " if preservation is not None:\n"
        "  if not isinstance(preservation,dict) or set(preservation)!={'connectionRef','rowSha256','requestIdSha256','operationIdSha256'}: raise RuntimeError('preserved UNKNOWN binding is malformed')\n"
        "  if [r[1] for r in db.execute('PRAGMA table_info(connector_invocations)')]!=invocation_columns or len(invocation_columns)!=20: raise RuntimeError('connector invocation schema differs from the sealed 20-column evidence')\n"
        " next_check_remaining=[]\n"
        " monitor_count=0\n"
        " for item in selected:\n"
        "  if not isinstance(item,dict) or set(item)!={'providerId','connectionRef'}: raise RuntimeError('selected monitor connection row is malformed')\n"
        "  provider=item['providerId']\n"
        "  connection_ref=item['connectionRef']\n"
        "  if provider not in stored_provider or not isinstance(connection_ref,str) or not connection_ref: raise RuntimeError('selected monitor provider is unsupported')\n"
        "  connection_rows=db.execute('SELECT document FROM integration_connections WHERE id=? AND organization_id=? AND workspace_id=? AND actor_issuer=? AND actor_subject=? AND enabled=1',(connection_ref,organization_id,workspace_id,issuer,subject)).fetchall()\n"
        "  if len(connection_rows)!=1: raise RuntimeError('selected connector connection is missing or ambiguous')\n"
        "  connection=json.loads(str(connection_rows[0][0]))\n"
        "  if not isinstance(connection,dict) or connection.get('id')!=connection_ref or connection.get('provider')!=stored_provider[provider] or connection.get('enabled') is not True or connection.get('organizationId')!=organization_id or connection.get('workspaceId')!=workspace_id or connection.get('actorIssuer')!=issuer or connection.get('actorSubject')!=subject: raise RuntimeError('selected connection is outside its exact provider actor scope')\n"
        "  monitor_rows=db.execute('SELECT next_check_at,claim_owner,lease_expires_at FROM authorization_monitor_state WHERE connection_id=?',(connection_ref,)).fetchall()\n"
        "  if len(monitor_rows)!=1: raise RuntimeError('selected authorization monitor state is missing or ambiguous')\n"
        "  monitor_count+=1\n"
        "  next_check_at=utc(monitor_rows[0][0],provider+' next_check_at')\n"
        "  claim_owner=monitor_rows[0][1]\n"
        "  lease_text=monitor_rows[0][2]\n"
        "  if claim_owner is not None and (not isinstance(claim_owner,str) or not claim_owner): raise RuntimeError(provider+' monitor claim owner is malformed')\n"
        "  if (claim_owner is None)!=(lease_text is None): raise RuntimeError(provider+' monitor lease fields are inconsistent')\n"
        "  if lease_text is not None:\n"
        "   lease_expiry=utc(lease_text,provider+' monitor lease expiry')\n"
        "   active_leases+=int(lease_expiry>now)\n"
        "  if preservation is None:\n"
        "   inflight_calls+=int(db.execute(\"SELECT COUNT(*) FROM connector_invocations WHERE organization_id=? AND workspace_id=? AND actor_issuer=? AND actor_subject=? AND connection_ref=? AND provider_id=? AND state NOT IN ('SUCCEEDED','FAILED','REJECTED','RATE_LIMITED')\",(organization_id,workspace_id,issuer,subject,connection_ref,provider)).fetchone()[0])\n"
        "  else:\n"
        "   rows=db.execute(\"SELECT * FROM connector_invocations WHERE connection_ref=? AND state NOT IN ('SUCCEEDED','FAILED','REJECTED','RATE_LIMITED')\",(connection_ref,)).fetchall()\n"
        "   for row in rows:\n"
        "    if len(row)!=len(invocation_columns): raise RuntimeError('connector invocation row width changed')\n"
        "    invocation=dict(zip(invocation_columns,row))\n"
        "    if invocation['provider_id']!=provider or invocation['organization_id']!=organization_id or invocation['workspace_id']!=workspace_id or invocation['actor_issuer']!=issuer or invocation['actor_subject']!=subject: raise RuntimeError('nonterminal connector invocation is outside the exact selected actor binding')\n"
        "    row_sha=hashlib.sha256(canonical([cell(value) for value in row])).hexdigest()\n"
        "    inflight_calls+=1\n"
        "    if provider=='github' and connection_ref==preservation['connectionRef'] and invocation['state']=='UNKNOWN_RESULT':\n"
        "     request_sha=hashlib.sha256(str(invocation['request_id']).encode()).hexdigest()\n"
        "     operation_sha=hashlib.sha256(str(invocation['operation_id']).encode()).hexdigest()\n"
        "     if row_sha!=preservation['rowSha256'] or request_sha!=preservation['requestIdSha256'] or operation_sha!=preservation['operationIdSha256'] or preserved_unknown_count!=0: raise RuntimeError('historical GitHub UNKNOWN row differs from its sealed evidence')\n"
        "     preserved_unknown_count=1\n"
        "     preserved_unknown_sha=row_sha\n"
        "  next_check_remaining.append(int((next_check_at-now).total_seconds()))\n"
        " if outbox_idle_required:\n"
        "  if [r[1] for r in db.execute('PRAGMA table_info(outbox_jobs)')]!=outbox_columns or len(outbox_columns)!=19: raise RuntimeError('Exchange outbox schema differs from the pinned worker ABI')\n"
        "  connection_ids=[item['connectionRef'] for item in selected]\n"
        "  if len(connection_ids)!=len(set(connection_ids)): raise RuntimeError('selected outbox connection set has duplicates')\n"
        "  placeholders=','.join('?' for _ in connection_ids)\n"
        "  scope_params=(organization_id,workspace_id,issuer,subject)\n"
        "  scope='organization_id=? AND workspace_id=? AND actor_issuer=? AND actor_subject=?'\n"
        "  def count_rows(query,parameters):\n"
        "   value=db.execute(query,parameters).fetchone()[0]\n"
        "   if isinstance(value,bool) or not isinstance(value,int) or value<0: raise RuntimeError('Exchange outbox count is malformed')\n"
        "   return value\n"
        "  now_text=now.isoformat()\n"
        "  unknown_connections=count_rows(\"SELECT COUNT(*) FROM outbox_jobs WHERE \"+scope+\" AND connection_id NOT IN (\"+placeholders+\") AND (state NOT IN ('ACCEPTED','SUCCEEDED','FAILED','UNKNOWN') OR (claim_owner IS NOT NULL AND lease_expires_at>?))\",scope_params+tuple(connection_ids)+(now_text,))\n"
        "  if unknown_connections: raise RuntimeError('Exchange outbox row is outside the exact selected actor connection set')\n"
        "  active_outbox_jobs=count_rows(\"SELECT COUNT(*) FROM outbox_jobs WHERE \"+scope+\" AND state NOT IN ('ACCEPTED','SUCCEEDED','FAILED','UNKNOWN')\",scope_params)\n"
        "  active_outbox_leases=count_rows('SELECT COUNT(*) FROM outbox_jobs WHERE '+scope+' AND claim_owner IS NOT NULL AND lease_expires_at>?',scope_params+(now_text,))\n"
        "  malformed_claim_fields=count_rows(\"SELECT COUNT(*) FROM outbox_jobs WHERE \"+scope+\" AND (state NOT IN ('ACCEPTED','SUCCEEDED','FAILED','UNKNOWN') OR (claim_owner IS NOT NULL AND lease_expires_at>?)) AND ((claim_owner IS NULL)!=(lease_expires_at IS NULL))\",scope_params+(now_text,))\n"
        "  if malformed_claim_fields: raise RuntimeError('Exchange outbox claim lease fields are inconsistent')\n"
        " cloud_refresh=0\n"
        " if cloud_refresh_required:\n"
        "  cloud_refresh=int(db.execute(\"SELECT COUNT(*) FROM cloud_refresh_outbox WHERE state IN ('QUEUED','RUNNING')\").fetchone()[0])\n"
        " report={'selectedProviderCount':len({item['providerId'] for item in selected}),'monitorStateCount':monitor_count,'activeMonitorLeaseCount':active_leases,'inFlightConnectorCallCount':inflight_calls,'preservedUnknownResultCount':preserved_unknown_count,'preservedUnknownRowSha256':preserved_unknown_sha,'activeOutboxJobCount':active_outbox_jobs,'activeOutboxJobLeaseCount':active_outbox_leases,'nextCheckSecondsRemaining':min(next_check_remaining),'requiredWindowSeconds':window,'nextCheckWindowSatisfied':all(value>window for value in next_check_remaining),'cloudRefreshNonterminalCount':cloud_refresh}\n"
        " print(json.dumps(report,sort_keys=True,separators=(',',':')))\n"
        "finally:\n"
        " db.close()\n"
    )
    try:
        output = runtime_update._checked_stdout(
            [
                *docker_prefix,
                "exec",
                "--user",
                "10001:10001",
                container_id,
                python,
                "-c",
                script,
                EXCHANGE_INTEGRATION_DATABASE,
                selected_connections_json,
                actor["issuer"],
                actor["subject"],
                actor["organizationId"],
                actor["workspaceId"],
                str(required_window_seconds),
                "true" if include_cloud_refresh else "false",
                preservation_json,
                invocation_columns_json,
                "true" if check_outbox_required else "false",
                outbox_columns_json,
            ],
            label="Connector authorization monitor quiescence readback",
            timeout_seconds=15,
        )
        report = runtime_update._decode_json_output(
            output, "connector authorization monitor quiescence report"
        )
    except (OSError, runtime_update.RuntimeUpdateError, subprocess.SubprocessError) as error:
        raise StageTransitionError(
            "connector authorization monitor quiescence could not be verified read-only"
        ) from error

    fields = {
        "selectedProviderCount",
        "monitorStateCount",
        "activeMonitorLeaseCount",
        "inFlightConnectorCallCount",
        "preservedUnknownResultCount",
        "preservedUnknownRowSha256",
        "activeOutboxJobCount",
        "activeOutboxJobLeaseCount",
        "nextCheckSecondsRemaining",
        "requiredWindowSeconds",
        "nextCheckWindowSatisfied",
        "cloudRefreshNonterminalCount",
    }
    if (
        not isinstance(report, dict)
        or set(report) != fields
        or any(
            isinstance(report.get(field), bool) or not isinstance(report.get(field), int)
            for field in (
                "selectedProviderCount",
                "monitorStateCount",
                "activeMonitorLeaseCount",
                "inFlightConnectorCallCount",
                "preservedUnknownResultCount",
                "activeOutboxJobCount",
                "activeOutboxJobLeaseCount",
                "nextCheckSecondsRemaining",
                "requiredWindowSeconds",
                "cloudRefreshNonterminalCount",
            )
        )
        or report.get("selectedProviderCount") != len(required_providers)
        or report.get("monitorStateCount") != expected_monitor_count
        or report.get("activeMonitorLeaseCount", -1) < 0
        or report.get("inFlightConnectorCallCount", -1) < 0
        or report.get("preservedUnknownResultCount", -1) < 0
        or report.get("activeOutboxJobCount", -1) < 0
        or report.get("activeOutboxJobLeaseCount", -1) < 0
        or report.get("requiredWindowSeconds") != required_window_seconds
        or report.get("cloudRefreshNonterminalCount", -1) < 0
        or (
            report.get("preservedUnknownRowSha256") is not None
            and (
                not isinstance(report.get("preservedUnknownRowSha256"), str)
                or SHA256_RE.fullmatch(report["preservedUnknownRowSha256"]) is None
            )
        )
        or not isinstance(report.get("nextCheckWindowSatisfied"), bool)
    ):
        raise StageTransitionError("connector authorization monitor report is malformed")

    expected_preserved_count = 1 if preserved_unknown is not None else 0
    expected_preserved_sha256 = (
        preserved_unknown.get("invocationRowSha256")
        if isinstance(preserved_unknown, dict)
        else None
    )
    active_in_flight_count = (
        report["inFlightConnectorCallCount"] - report["preservedUnknownResultCount"]
    )
    if active_in_flight_count < 0:
        raise StageTransitionError("connector monitor report has an impossible UNKNOWN count")
    verified = (
        report["activeMonitorLeaseCount"] == 0
        and active_in_flight_count == 0
        and report["inFlightConnectorCallCount"] == expected_preserved_count
        and report["preservedUnknownResultCount"] == expected_preserved_count
        and report["preservedUnknownRowSha256"] == expected_preserved_sha256
        and report["activeOutboxJobCount"] == 0
        and report["activeOutboxJobLeaseCount"] == 0
        and report["nextCheckWindowSatisfied"] is True
        and report["cloudRefreshNonterminalCount"] == 0
        and all(value == 0 for value in active_gh_process_count_by_service.values())
        and (target != ACCOUNT_CENTER_IMAGE_UPDATE_TARGET or active_host_gh_process_count == 0)
    )
    reference_hashes = {
        provider: [_sha256_bytes(connection_ref.encode("utf-8")) for connection_ref in refs]
        if len(refs) > 1
        else _sha256_bytes(refs[0].encode("utf-8"))
        for provider, refs in sorted(canonical_refs.items())
    }
    evidence = {
        "verified": verified,
        "connectionRefsSha256": _sha256_bytes(_canonical_json(reference_hashes)),
        "activeMonitorLeaseCount": report["activeMonitorLeaseCount"],
        "inFlightConnectorCallCount": report["inFlightConnectorCallCount"],
        "rawNonterminalConnectorInvocationCount": report["inFlightConnectorCallCount"],
        "preservedUnknownResultCount": report["preservedUnknownResultCount"],
        "activeInFlightConnectorCallCount": active_in_flight_count,
        "preservedUnknownRowSha256": report["preservedUnknownRowSha256"],
        "preservedUnknownStatus": (
            "PRESERVED_UNKNOWN_NOT_RESOLVED"
            if preserved_unknown is not None
            else "NO_UNKNOWN_RESULT"
        ),
        "activeGitHubCliProcessCountByService": active_gh_process_count_by_service,
        "activeHostGitHubCliProcessCount": active_host_gh_process_count,
        "activeOutboxJobCount": report["activeOutboxJobCount"],
        "activeOutboxJobLeaseCount": report["activeOutboxJobLeaseCount"],
        "nextCheckSecondsRemaining": report["nextCheckSecondsRemaining"],
        "requiredWindowSeconds": required_window_seconds,
        "cloudRefreshNonterminalCount": report["cloudRefreshNonterminalCount"],
        "checkedAtUtc": _utc_now(),
    }
    update_state = journal.get(target_journal_key)
    if not isinstance(update_state, dict) or update_state.get("required") is not True:
        raise StageTransitionError("connector image update journal state is malformed")
    if target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET and checkpoint == "after":
        update_state["afterMonitorQuiescence"] = evidence
    else:
        update_state["monitorQuiescence"] = evidence
    _write_journal(
        journal_path,
        journal,
        "CONNECTOR_MONITOR_QUIESCENCE_VERIFIED" if verified else "CONNECTOR_MONITOR_NOT_QUIESCENT",
    )
    if not verified:
        if report["activeMonitorLeaseCount"]:
            reason = "a live provider authorization monitor lease exists"
        elif active_in_flight_count:
            reason = "a nonterminal provider connector invocation exists besides the sealed UNKNOWN result"
        elif report["preservedUnknownResultCount"] != expected_preserved_count:
            reason = "the exact sealed UNKNOWN result is not still present"
        elif any(active_gh_process_count_by_service.values()):
            reason = "a GitHub CLI process could replay the preserved UNKNOWN operation"
        elif active_host_gh_process_count:
            reason = "a host GitHub CLI process could issue a provider operation"
        elif report["activeOutboxJobCount"] or report["activeOutboxJobLeaseCount"]:
            reason = "Exchange has selected-actor connector jobs or active dispatcher leases"
        elif report["cloudRefreshNonterminalCount"]:
            reason = "the Exchange cloud refresh outbox is not empty"
        else:
            reason = "a provider authorization check is inside the image health window"
        raise StageTransitionError(f"connector image update is blocked because {reason}")
    return evidence


def _cloud_runner_state_record(
    *,
    active_compose: dict[str, Any],
    candidate_compose: dict[str, Any],
    exchange_service: dict[str, Any],
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Bind the disabled runner state to the exact active Exchange container.

    中文：候选和活动配置以及活动容器都必须指向同一关闭状态。
    """

    active_value = _exchange_cloud_runner_compose_value(active_compose)
    candidate_value = _exchange_cloud_runner_compose_value(candidate_compose)
    container_id = exchange_service.get("containerId")
    image_id = exchange_service.get("imageId")
    if (
        not isinstance(container_id, str)
        or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None
        or not isinstance(image_id, str)
        or IMAGE_ID_RE.fullmatch(image_id) is None
    ):
        raise StageTransitionError("active Exchange container identity is malformed")
    integration_database_path = _exchange_integration_database_container_path(
        container_id=container_id, docker_prefix=docker_prefix
    )
    live_value = _exchange_cloud_runner_container_value(
        container_id=container_id, docker_prefix=docker_prefix
    )
    idle_counts = _exchange_cloud_runner_idle_counts(
        container_id=container_id, docker_prefix=docker_prefix
    )
    return {
        "exchangeContainerId": container_id,
        "exchangeImageId": image_id,
        "integrationDatabasePath": integration_database_path,
        "environmentName": EXCHANGE_CLOUD_RUNNER_ENV,
        "activeComposeValue": active_value,
        "candidateComposeValue": candidate_value,
        "liveContainerValue": live_value,
        "idleCounts": idle_counts,
    }


def _validate_cloud_runner_state_record(
    value: Any, active_services: dict[str, Any]
) -> dict[str, Any]:
    """Validate the closed, secret-free runner quiescence evidence in one plan.

    中文：计划中的 Exchange 容器身份必须与活动服务快照完全一致。
    """

    fields = {
        "exchangeContainerId",
        "exchangeImageId",
        "integrationDatabasePath",
        "environmentName",
        "activeComposeValue",
        "candidateComposeValue",
        "liveContainerValue",
        "idleCounts",
    }
    exchange = active_services.get("exchange")
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or not isinstance(exchange, dict)
        or value.get("exchangeContainerId") != exchange.get("containerId")
        or value.get("exchangeImageId") != exchange.get("imageId")
        or value.get("environmentName") != EXCHANGE_CLOUD_RUNNER_ENV
        or any(
            value.get(field) != "false"
            for field in (
                "activeComposeValue",
                "candidateComposeValue",
                "liveContainerValue",
            )
        )
    ):
        raise StageTransitionError("Cloud image update runner-state evidence is malformed")
    container_id = value.get("exchangeContainerId")
    image_id = value.get("exchangeImageId")
    if (
        not isinstance(container_id, str)
        or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None
        or not isinstance(image_id, str)
        or IMAGE_ID_RE.fullmatch(image_id) is None
        or value.get("integrationDatabasePath") != EXCHANGE_INTEGRATION_DATABASE
    ):
        raise StageTransitionError("Cloud image update runner-state evidence is malformed")
    idle_counts = value.get("idleCounts")
    count_fields = {
        "cloudRefreshNonterminalCount",
        "cloudRefreshActiveLeaseCount",
        "authorizationMonitorActiveLeaseCount",
        "emailNotificationNonterminalCount",
    }
    if (
        not isinstance(idle_counts, dict)
        or set(idle_counts) != count_fields
        or any(
            isinstance(idle_counts.get(field), bool)
            or not isinstance(idle_counts.get(field), int)
            or idle_counts[field] != 0
            for field in count_fields
        )
    ):
        raise StageTransitionError("Cloud image update worker queues are not idle")
    return dict(value)


def _verify_cloud_runner_idle(
    *,
    plan: dict[str, Any],
    active_compose: dict[str, Any],
    candidate_compose: dict[str, Any],
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Recheck the frozen runner-off and durable-job-idle contract.

    中文：每个切换边界都复核 runner 仍关闭且持久队列与租约仍为空。
    """

    expected = _validate_cloud_runner_state_record(
        plan.get("cloudRunnerState"), plan["activeServices"]
    )
    exchange_service = plan["activeServices"].get("exchange")
    if not isinstance(exchange_service, dict):
        raise StageTransitionError("active Exchange service is missing for Cloud image update")
    observed = _cloud_runner_state_record(
        active_compose=active_compose,
        candidate_compose=candidate_compose,
        exchange_service=exchange_service,
        docker_prefix=docker_prefix,
    )
    if observed != expected:
        raise StageTransitionError(
            "Exchange runner or durable cloud/email work changed after Cloud update planning"
        )
    return observed


def _service_command_readback(*, container_id: str, docker_prefix: list[str]) -> list[str]:
    """Read the exact command array for one sealed provider container.

    中文：只读取目标容器的命令参数，不使用容器名或宽泛查询。
    """

    if FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError("provider listener container ID is malformed")
    try:
        output = runtime_update._checked_stdout(
            [
                *docker_prefix,
                "inspect",
                "--format",
                "{{json .Config.Cmd}}",
                container_id,
            ],
            label="provider listener command readback",
        )
        command = runtime_update._decode_json_output(output, "provider listener command")
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "provider listener command could not be verified from its exact container"
        ) from error
    if not isinstance(command, list) or any(not isinstance(value, str) for value in command):
        raise StageTransitionError("provider listener command readback is malformed")
    return command


def _docker_environment_sha256(value: Any) -> str:
    """Hash a normalized Docker environment without retaining any variable values.

    中文：仅在私有计划中绑定环境读回，绝不持久化或输出环境变量原文。
    """

    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise StageTransitionError("provider listener environment readback is malformed")
    environment: dict[str, str] = {}
    for item in value:
        name, separator, content = item.partition("=")
        if not separator or not name or "\x00" in name or name in environment:
            raise StageTransitionError("provider listener environment readback is ambiguous")
        environment[name] = content
    return _sha256_bytes(_canonical_json(environment))


def _service_environment_sha256(*, container_id: str, docker_prefix: list[str]) -> str:
    """Read and immediately hash one exact provider container's environment."""

    if FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError("provider listener container ID is malformed")
    try:
        output = runtime_update._checked_stdout(
            [
                *docker_prefix,
                "inspect",
                "--format",
                "{{json .Config.Env}}",
                container_id,
            ],
            label="provider listener environment readback",
        )
        environment = runtime_update._decode_json_output(output, "provider listener environment")
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "provider listener environment could not be verified from its exact container"
        ) from error
    return _docker_environment_sha256(environment)


def _listener_repair_state_record(
    *,
    active_compose: dict[str, Any],
    candidate_compose: dict[str, Any],
    active_services: dict[str, Any],
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Bind both default commands and exact containers to the candidate wildcard commands.

    中文：计划要求实际旧命令仍绑定 hostname，候选只把 listen 参数改为通配地址。
    """

    records: dict[str, dict[str, Any]] = {}
    for service_name in PROVIDER_LISTENER_REPAIR_TARGETS:
        active_definition = active_compose.get("services", {}).get(service_name)
        candidate_definition = candidate_compose.get("services", {}).get(service_name)
        live = active_services.get(service_name)
        if not isinstance(active_definition, dict) or not isinstance(candidate_definition, dict):
            raise StageTransitionError(f"provider listener service {service_name} is missing")
        if not isinstance(live, dict):
            raise StageTransitionError(
                f"active provider listener service {service_name} is missing"
            )
        active_command = active_definition.get("command")
        if active_command is not None and active_command != list(
            PROVIDER_DEFAULT_COMMANDS[service_name]
        ):
            raise StageTransitionError(
                f"active {service_name} Compose command is not the hostname-bound default"
            )
        candidate_command = candidate_definition.get("command")
        if candidate_command != list(PROVIDER_WILDCARD_COMMANDS[service_name]):
            raise StageTransitionError(
                f"candidate {service_name} Compose command is not the fixed wildcard command"
            )
        container_id = live.get("containerId")
        image_id = live.get("imageId")
        if (
            not isinstance(container_id, str)
            or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None
            or not isinstance(image_id, str)
            or IMAGE_ID_RE.fullmatch(image_id) is None
        ):
            raise StageTransitionError(f"active {service_name} container identity is malformed")
        observed_command = _service_command_readback(
            container_id=container_id,
            docker_prefix=docker_prefix,
        )
        if observed_command != list(PROVIDER_DEFAULT_COMMANDS[service_name]):
            raise StageTransitionError(
                f"active {service_name} container command is not the exact hostname-bound default"
            )
        records[service_name] = {
            "containerId": container_id,
            "imageId": image_id,
            "activeCommand": observed_command,
            "candidateCommand": list(candidate_command),
            "environmentSha256": _service_environment_sha256(
                container_id=container_id, docker_prefix=docker_prefix
            ),
        }
    return {"services": records}


def _validate_listener_repair_state_record(
    value: Any, active_services: dict[str, Any]
) -> dict[str, Any]:
    """Validate the closed command/container record for the two provider targets."""

    if not isinstance(value, dict) or set(value) != {"services", "issuedState"}:
        raise StageTransitionError("provider listener repair state is malformed")
    services = value.get("services")
    if not isinstance(services, dict) or set(services) != set(PROVIDER_LISTENER_REPAIR_TARGETS):
        raise StageTransitionError("provider listener repair service set is malformed")
    validated: dict[str, dict[str, Any]] = {}
    for service_name in PROVIDER_LISTENER_REPAIR_TARGETS:
        row = services.get(service_name)
        active = active_services.get(service_name)
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "containerId",
                "imageId",
                "activeCommand",
                "candidateCommand",
                "environmentSha256",
            }
            or not isinstance(active, dict)
            or row.get("containerId") != active.get("containerId")
            or row.get("imageId") != active.get("imageId")
            or row.get("activeCommand") != list(PROVIDER_DEFAULT_COMMANDS[service_name])
            or row.get("candidateCommand") != list(PROVIDER_WILDCARD_COMMANDS[service_name])
            or not isinstance(row.get("environmentSha256"), str)
            or SHA256_RE.fullmatch(row["environmentSha256"]) is None
        ):
            raise StageTransitionError("provider listener repair command identity is malformed")
        validated[service_name] = dict(row)
    issued = value.get("issuedState")
    issued_fields = {
        "exchangeContainerId",
        "exchangeImageId",
        "issuedStateSha256",
        "catalogStatus",
        "profileCount",
        "connectionCount",
        "bindingCount",
        "verifiedProviderIds",
    }
    exchange = active_services.get("exchange")
    if (
        not isinstance(issued, dict)
        or set(issued) != issued_fields
        or not isinstance(exchange, dict)
        or issued.get("exchangeContainerId") != exchange.get("containerId")
        or issued.get("exchangeImageId") != exchange.get("imageId")
        or issued.get("catalogStatus") != "NOT_LIVE_CATALOG"
        or not isinstance(issued.get("issuedStateSha256"), str)
        or SHA256_RE.fullmatch(issued["issuedStateSha256"]) is None
        or any(
            isinstance(issued.get(field), bool)
            or not isinstance(issued.get(field), int)
            or issued[field] < 1
            for field in ("profileCount", "connectionCount", "bindingCount")
        )
        or issued.get("profileCount") != 5
        or issued.get("connectionCount") != 14
        or issued.get("bindingCount") != 14
        or issued.get("verifiedProviderIds")
        != ["azure", "computer", "gcp", "github", "huggingface"]
    ):
        raise StageTransitionError("listener repair issued-state evidence is malformed")
    return {"services": validated, "issuedState": dict(issued)}


def _exchange_issued_metadata_sha256(snapshot: dict[str, Any]) -> str:
    """Hash actor-scoped issue state, excluding catalog-derived maps and flags.

    中文：Cloud readOnly 标记会随真实目录变化，权限仍由 server config 与 post-readiness 核验。
    """

    profiles = snapshot.get("profiles")
    connections = snapshot.get("connections")
    if not isinstance(profiles, list) or not isinstance(connections, list):
        raise StageTransitionError("Exchange issued profile metadata is malformed")
    stable_profiles = []
    for source in profiles:
        row = dict(source)
        if row.get("providerId") in {"azure", "gcp", "runpod"}:
            row.pop("readOnly", None)
        stable_profiles.append(row)
    stable_connections = []
    for source in connections:
        row = dict(source)
        if row.get("providerId") in {"azure", "gcp", "runpod"}:
            row.pop("readOnly", None)
        stable_connections.append(row)
    projection = {
        "actor": snapshot.get("actor"),
        "profiles": sorted(
            stable_profiles,
            key=lambda row: (row["providerId"], row["profileRef"]),
        ),
        "connections": sorted(
            stable_connections,
            key=lambda row: (row["providerId"], row["connectionRef"]),
        ),
        "bindings": [
            {key: row[key] for key in ("bindingId", "connectionRef", "providerId", "profileRef")}
            for row in _sorted_exchange_bindings(snapshot.get("bindings"))
        ],
    }
    return _sha256_bytes(_canonical_json(projection))


def _read_live_exchange_issued_state(
    *,
    compose: dict[str, Any],
    container_id: str,
    image_id: str,
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Read Store bindings and Account Center scopes without calling provider ListTools.

    中文：只用 Exchange 的真实 actor-scoped store/profile 导出，目录明确标为未读取。
    """

    if (
        FULL_CONTAINER_ID_RE.fullmatch(container_id) is None
        or IMAGE_ID_RE.fullmatch(image_id) is None
    ):
        raise StageTransitionError("Exchange issued-state container identity is malformed")
    actor = preflight._connector_bootstrap_actor(compose)
    if any(not isinstance(value, str) or not value for value in actor.values()):
        raise StageTransitionError("candidate Exchange issued-state actor is incomplete")
    python = str(Path(EXCHANGE_READINESS_EXECUTABLE).with_name("python"))
    argv = [
        *docker_prefix,
        "exec",
        container_id,
        EXCHANGE_SECRET_ENV_ENTRYPOINT,
        python,
        "-c",
        _EXCHANGE_ISSUED_STATE_PYTHON,
        EXCHANGE_INTEGRATION_DATABASE,
        actor["issuer"],
        actor["subject"],
        actor["organizationId"],
        actor["workspaceId"],
    ]
    try:
        output = runtime_update._checked_stdout(
            argv,
            label="Exchange issued connector metadata readback",
            timeout_seconds=120,
        )
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "Exchange issued connector metadata could not be verified without ListTools"
        ) from error
    if len(output.encode("utf-8")) > 16 * 1024 * 1024:
        raise StageTransitionError("Exchange issued connector metadata exceeds its bounded size")
    try:
        snapshot = json.loads(output, object_pairs_hook=preflight._unique_json_object)
    except (json.JSONDecodeError, ValueError) as error:
        raise StageTransitionError("Exchange issued connector metadata is invalid JSON") from error
    receipt = _verify_exchange_readiness_snapshot(
        snapshot=snapshot,
        compose=compose,
        phase="provider-bootstrap",
        listener_repair=True,
        include_live_catalog=False,
    )
    receipt["exchangeContainerId"] = container_id
    receipt["exchangeImageId"] = image_id
    receipt["issuedStateSha256"] = _exchange_issued_metadata_sha256(snapshot)
    receipt["catalogStatus"] = "NOT_LIVE_CATALOG"
    return receipt


def _verify_listener_repair_active_state(
    *,
    plan: dict[str, Any],
    active_compose: dict[str, Any],
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Recheck exact old provider commands and actor-scoped issued metadata before replacement."""

    expected = _validate_listener_repair_state_record(
        plan.get("listenerRepairState"), plan["activeServices"]
    )
    for service_name, row in expected["services"].items():
        observed = _service_command_readback(
            container_id=row["containerId"], docker_prefix=docker_prefix
        )
        if observed != row["activeCommand"]:
            raise StageTransitionError(
                f"active {service_name} listener command changed after planning"
            )
        if (
            _service_environment_sha256(
                container_id=row["containerId"], docker_prefix=docker_prefix
            )
            != row["environmentSha256"]
        ):
            raise StageTransitionError(
                f"active {service_name} listener environment changed after planning"
            )
    exchange = plan["activeServices"].get("exchange")
    if not isinstance(exchange, dict):
        raise StageTransitionError("active Exchange service is missing for listener repair")
    receipt = _read_live_exchange_issued_state(
        compose=active_compose,
        container_id=exchange["containerId"],
        image_id=exchange["imageId"],
        docker_prefix=docker_prefix,
    )
    expected_issued = plan["listenerRepairState"]["issuedState"]
    if any(
        receipt.get(field) != expected_issued.get(field)
        for field in (
            "exchangeContainerId",
            "exchangeImageId",
            "issuedStateSha256",
            "catalogStatus",
            "profileCount",
            "connectionCount",
            "bindingCount",
            "verifiedProviderIds",
        )
    ):
        raise StageTransitionError(
            "Exchange issued connections or Account Center scopes changed after listener repair planning"
        )
    return receipt


def _verify_listener_repair_target_commands(
    *, plan: dict[str, Any], journal: dict[str, Any], docker_prefix: list[str]
) -> None:
    """Require both recreated provider containers to run the exact wildcard command."""

    state = _validate_listener_repair_state_record(
        plan.get("listenerRepairState"), plan["activeServices"]
    )
    for service_name, row in state["services"].items():
        progress = journal["targetProgress"].get(service_name)
        container_id = progress.get("candidateContainerId") if isinstance(progress, dict) else None
        if (
            not isinstance(container_id, str)
            or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None
        ):
            raise StageTransitionError(f"updated {service_name} container ID is missing")
        observed = _service_command_readback(container_id=container_id, docker_prefix=docker_prefix)
        if observed != row["candidateCommand"]:
            raise StageTransitionError(
                f"updated {service_name} container does not run the exact wildcard listener command"
            )
        if (
            _service_environment_sha256(container_id=container_id, docker_prefix=docker_prefix)
            != row["environmentSha256"]
        ):
            raise StageTransitionError(
                f"updated {service_name} listener environment differs from the active container"
            )


def _mount_rows(service: dict[str, Any], field: str) -> list[dict[str, Any]]:
    """Return validated Compose mount rows for one service field."""

    rows = service.get(field, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise StageTransitionError(f"Compose {field} list is malformed")
    return rows


def _volume_source_for_target(service: dict[str, Any], target: str, *, kind: str) -> str:
    """Require exactly one named volume or bind mount at a fixed container target."""

    matches = [
        row
        for row in _mount_rows(service, "volumes")
        if row.get("target") == target and row.get("type") == kind
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("source"), str):
        raise StageTransitionError(f"required {kind} mount {target} is missing or ambiguous")
    return matches[0]["source"]


def _validate_durable_mounts(active: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Keep the Navigator database volume and CodeBuddy profile at the same host paths."""

    for compose in (active, candidate):
        services = compose.get("services", {})
        persistence = services.get("navigator-persistence")
        navigator = services.get("navigator")
        if not isinstance(persistence, dict) or not isinstance(navigator, dict):
            raise StageTransitionError("Navigator Persistence or executor service is missing")
        if (
            _volume_source_for_target(persistence, DATABASE_VOLUME_TARGET, kind="volume")
            != "navigator-sessions"
        ):
            raise StageTransitionError(
                "Navigator Persistence must keep the navigator-sessions volume"
            )
        if _volume_source_for_target(navigator, DATABASE_VOLUME_TARGET, kind="volume") != (
            "navigator-sessions"
        ):
            raise StageTransitionError("Navigator executor must keep the navigator-sessions volume")
        environment = persistence.get("environment", {})
        if environment.get("NAVIGATOR_DATABASE_PATH") != DATABASE_PATH:
            raise StageTransitionError("Navigator Persistence database path changed")
    active_home = _volume_source_for_target(
        active["services"]["navigator"], CODEBUDDY_HOME_TARGET, kind="bind"
    )
    candidate_home = _volume_source_for_target(
        candidate["services"]["navigator"], CODEBUDDY_HOME_TARGET, kind="bind"
    )
    if active_home != candidate_home:
        raise StageTransitionError("the service-owned CodeBuddy profile path must remain stable")


def _mounted_file_digests(compose: dict[str, Any], service_name: str) -> dict[str, str]:
    """Return private content digests for one service's Compose config and secret files."""

    service = compose.get("services", {}).get(service_name)
    if not isinstance(service, dict):
        return {}
    result: dict[str, str] = {}
    for field, collection in (
        ("configs", compose.get("configs", {})),
        ("secrets", compose.get("secrets", {})),
    ):
        if not isinstance(collection, dict):
            raise StageTransitionError(f"Compose {field} inventory is malformed")
        for row in _mount_rows(service, field):
            source_name = row.get("source")
            source = collection.get(source_name) if isinstance(source_name, str) else None
            file_path = source.get("file") if isinstance(source, dict) else None
            if not isinstance(source_name, str) or not isinstance(file_path, str):
                raise StageTransitionError(f"{service_name} mounts an unresolved {field} file")
            result[f"{field}:{source_name}"] = _sha256_file(Path(file_path))
    return result


def _validate_phase_topology(
    *,
    phase: str,
    image_update_target: str | None = None,
    memory_update_target: str | None = None,
    listener_repair: bool = False,
    active: dict[str, Any],
    candidate: dict[str, Any],
    active_lock: dict[str, Any],
    candidate_lock: dict[str, Any],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Enforce the exact service delta, retained single-executor boundary, and image phase."""

    if phase not in PHASES:
        raise StageTransitionError(
            "phase must be provider-bootstrap or issued-connector-activation; Microsoft Graph provider bootstrap is also supported"
        )
    if active.get("name") != candidate.get("name"):
        raise StageTransitionError("active and candidate Compose project names differ")
    project = active.get("name")
    if not isinstance(project, str) or PROJECT_RE.fullmatch(project) is None:
        raise StageTransitionError("Compose project name is malformed")
    _validate_durable_mounts(active, candidate)
    _validate_compose_definition_deltas(active, candidate)
    old_services = _runtime_service_inventory(active)
    new_services = _runtime_service_inventory(candidate)
    if listener_repair:
        if image_update_target is not None or memory_update_target is not None:
            raise StageTransitionError("listener repair cannot select another update target")
        return _validate_listener_repair_topology(
            phase=phase,
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
            old_services=old_services,
            new_services=new_services,
        )
    if image_update_target is not None and memory_update_target is not None:
        raise StageTransitionError("image and memory update modes are mutually exclusive")
    if memory_update_target is not None:
        return _validate_memory_update_topology(
            phase=phase,
            target=memory_update_target,
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
            old_services=old_services,
            new_services=new_services,
        )
    if image_update_target is not None:
        return _validate_image_update_topology(
            phase=phase,
            target=image_update_target,
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
            old_services=old_services,
            new_services=new_services,
        )
    if phase == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE:
        return _validate_office_graph_provider_bootstrap_topology(
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
            old_services=old_services,
            new_services=new_services,
        )
    if phase == "provider-bootstrap":
        added = set(new_services) - set(old_services)
        removed = set(old_services) - set(new_services)
        if added != NEW_SERVICES or removed:
            raise StageTransitionError(
                "provider bootstrap must add only Hugging Face and Cloud Management without removing services"
            )
    elif phase == "issued-connector-activation":
        if _runtime_service_shape(
            old_services, "active issued activation"
        ) != _runtime_service_shape(new_services, "candidate issued activation") or set(
            new_services
        ) != set(old_services):
            raise StageTransitionError(
                "issued connector activation must preserve the exact supported runtime services"
            )
    if sum(name == "navigator" for name in new_services) != 1:
        raise StageTransitionError(
            "the transition must retain exactly one Navigator executor service"
        )

    active_ids = _lock_image_ids(active_lock)
    candidate_ids = _lock_image_ids(candidate_lock)
    try:
        active_rows = runtime_update._image_rows_by_service(active_lock)
        candidate_rows = runtime_update._image_rows_by_service(candidate_lock)
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "active or candidate built image inventory is malformed"
        ) from error
    mutable_lock_services = {
        "navigator-persistence",
        "account-center",
        "huggingface-provider",
        "cloud-management",
        "exchange",
        "dsh-executor",
    }
    if not set(active_rows) <= set(candidate_rows):
        raise StageTransitionError("candidate image lock removes a previously locked build service")
    for service_name, row in active_rows.items():
        if service_name not in mutable_lock_services and candidate_rows[service_name] != row:
            raise StageTransitionError(f"candidate changes non-target built image {service_name}")
    if not (set(candidate_rows) - set(active_rows)) <= {"huggingface-provider", "cloud-management"}:
        raise StageTransitionError("candidate adds an image outside the HF/cloud provider set")
    if active_lock.get("externalImages") != candidate_lock.get("externalImages"):
        raise StageTransitionError("candidate changes non-target external image locks")
    target_images: dict[str, dict[str, Any]] = {}
    changed: set[str] = set()
    for name, candidate_service in new_services.items():
        candidate_image = _service_image(candidate, candidate_ids, name)
        old_service = old_services.get(name)
        old_image = _service_image(active, active_ids, name) if old_service is not None else None
        if phase == "issued-connector-activation" and (
            old_image is None or old_image != candidate_image
        ):
            raise StageTransitionError(
                "issued connector activation must keep every image ID unchanged"
            )
        config_changed = old_service is None or old_service != candidate_service
        mounted_files_changed = old_service is not None and _mounted_file_digests(
            active, name
        ) != _mounted_file_digests(candidate, name)
        image_changed = old_image is None or old_image != candidate_image
        if config_changed or mounted_files_changed or image_changed:
            if name not in SERVICE_SET:
                raise StageTransitionError(f"candidate changes non-target runtime service {name}")
            changed.add(name)
        target_images[name] = {
            "active": old_image,
            "candidate": candidate_image,
            "added": old_service is None,
            "configChanged": config_changed or mounted_files_changed,
        }

    for name, old_service in old_services.items():
        if name not in new_services:
            continue
        if name not in SERVICE_SET and old_service != new_services[name]:
            raise StageTransitionError(f"candidate changes non-target runtime service {name}")
    if phase == "provider-bootstrap":
        persistence = target_images.get("navigator-persistence")
        if not isinstance(persistence, dict) or persistence["active"] is None:
            raise StageTransitionError(
                "provider bootstrap requires the existing locked Persistence service"
            )
    ordered_targets = [name for name in SERVICE_ORDER if name in changed]
    if not ordered_targets or "navigator" not in ordered_targets:
        raise StageTransitionError(
            "transition must update Navigator after its connector catalog/config change"
        )
    if phase == "provider-bootstrap" and not NEW_SERVICES <= set(ordered_targets):
        raise StageTransitionError(
            "provider bootstrap must include both newly locked provider services"
        )
    return ordered_targets, target_images


def _validate_office_graph_provider_bootstrap_topology(
    *,
    active: dict[str, Any],
    candidate: dict[str, Any],
    active_lock: dict[str, Any],
    candidate_lock: dict[str, Any],
    old_services: dict[str, dict[str, Any]],
    new_services: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Add Graph, pin its service images, and apply the exact Cloud memory reallocation."""

    if (
        set(old_services) != RUNTIME_SERVICE_SET
        or set(new_services) != OFFICE_GRAPH_RUNTIME_SERVICE_SET
        or set(new_services) - set(old_services) != {"microsoft-graph"}
        or set(old_services) - set(new_services)
    ):
        raise StageTransitionError(
            "Microsoft Graph provider bootstrap must add exactly one service to the active 14-service runtime"
        )
    if sum(name == "navigator" for name in new_services) != 1:
        raise StageTransitionError(
            "Graph provider bootstrap must retain exactly one Navigator executor"
        )

    try:
        active_rows = runtime_update._image_rows_by_service(active_lock)
        candidate_rows = runtime_update._image_rows_by_service(candidate_lock)
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "active or candidate Graph image inventory is malformed"
        ) from error
    if not set(active_rows) <= set(candidate_rows):
        raise StageTransitionError("Graph candidate image lock removes an existing build service")
    if set(candidate_rows) - set(active_rows) != {"microsoft-graph"}:
        raise StageTransitionError("Graph candidate must add only the Microsoft Graph image")
    mutable_lock_services = {
        "account-center",
        "exchange",
        "dsh-executor",
        "navigator-persistence",
        "teams-ingress",
        "microsoft-graph",
    }
    for service_name, row in active_rows.items():
        if service_name not in mutable_lock_services and candidate_rows[service_name] != row:
            raise StageTransitionError(f"Graph bootstrap changes non-target image {service_name}")
    if active_lock.get("externalImages") != candidate_lock.get("externalImages"):
        raise StageTransitionError("Graph bootstrap changes a non-target external image")
    if any(
        active_rows.get(service) == candidate_rows.get(service)
        for service in (
            "account-center",
            "exchange",
            "dsh-executor",
            "navigator-persistence",
            "teams-ingress",
        )
    ):
        raise StageTransitionError(
            "Graph bootstrap requires the pinned Account Center, Exchange, and Navigator image updates"
        )

    expected_targets = {
        "account-center",
        "exchange",
        "navigator",
        "navigator-persistence",
        "teams-provider",
        "teams-ingress",
        "microsoft-graph",
        "cloud-management-phase2",
    }
    active_ids = _lock_image_ids(active_lock)
    candidate_ids = _lock_image_ids(candidate_lock)
    target_images: dict[str, dict[str, Any]] = {}
    changed: set[str] = set()
    for name, candidate_service in new_services.items():
        old_service = old_services.get(name)
        old_image = _service_image(active, active_ids, name) if old_service is not None else None
        candidate_image = _service_image(candidate, candidate_ids, name)
        config_changed = old_service is None or old_service != candidate_service
        mounted_files_changed = old_service is not None and _mounted_file_digests(
            active, name
        ) != _mounted_file_digests(candidate, name)
        image_changed = old_image is None or old_image != candidate_image
        if config_changed or mounted_files_changed or image_changed:
            if name not in expected_targets:
                raise StageTransitionError(
                    f"Graph bootstrap changes non-target runtime service {name}"
                )
            changed.add(name)
        target_images[name] = {
            "active": old_image,
            "candidate": candidate_image,
            "added": old_service is None,
            "configChanged": config_changed or mounted_files_changed,
        }
    if changed != expected_targets:
        raise StageTransitionError(
            "Graph bootstrap must update the four pinned services, add Graph, and reallocate Cloud memory"
        )
    if target_images["microsoft-graph"]["added"] is not True:
        raise StageTransitionError("Graph bootstrap must add the new provider process")
    ordered_targets = [name for name in OFFICE_GRAPH_STAGE_TARGET_ORDER if name in changed]
    if set(ordered_targets) != expected_targets:
        raise StageTransitionError("Graph bootstrap target order omits a required service")
    _validate_office_graph_provider_compose_delta(active, candidate)
    return ordered_targets, target_images


def _validate_office_graph_provider_compose_delta(
    active: dict[str, Any], candidate: dict[str, Any]
) -> None:
    """Allow Graph setup plus only the reviewed 384-to-256 MiB Cloud memory change."""

    graph_networks = {
        "graph-exchange-private",
        "graph-account-center-private",
        "graph-egress",
    }
    graph_secret_names = {
        "account-center-graph-provider-hmac-key",
        "account-center-microsoft-graph-callback-hmac-key",
        "account-center-microsoft-graph-client-secret",
        "exchange-graph-auth-context-hmac-key",
        "microsoft-graph-server-cert",
        "microsoft-graph-server-key",
        "microsoft-graph-health-client-cert",
        "microsoft-graph-health-client-key",
        "microsoft-graph-account-center-client-cert",
        "microsoft-graph-account-center-client-key",
    }
    graph_account_secret = (
        "account-center-graph-provider-hmac-key",
        "account-center-graph-provider-hmac-key",
    )
    graph_callback_secret = (
        "account-center-microsoft-graph-callback-hmac-key",
        "account-center-microsoft-graph-callback-hmac-key",
    )
    graph_client_secret = (
        "account-center-microsoft-graph-client-secret",
        "account-center-microsoft-graph-client-secret",
    )
    graph_exchange_secret = (
        "exchange-graph-auth-context-hmac-key",
        "exchange-graph-auth-context-hmac-key",
    )
    account_map_key = "CYRENE_ACCOUNT_CENTER_GRAPH_PROVIDER_HMAC_KEY"
    account_callback_map_key = "CYRENE_ACCOUNT_CENTER_MICROSOFT_GRAPH_CALLBACK_HMAC_KEY"
    account_client_map_key = "CYRENE_ACCOUNT_CENTER_MICROSOFT_GRAPH_CLIENT_SECRET"
    exchange_map_key = "EXCHANGE_GRAPH_AUTH_CONTEXT_HMAC_KEY"
    exchange_callback_map_key = "CYRENE_ACCOUNT_CENTER_MICROSOFT_GRAPH_CALLBACK_HMAC_KEY"

    active_services = copy.deepcopy(active.get("services", {}))
    candidate_services = copy.deepcopy(candidate.get("services", {}))
    if not isinstance(active_services, dict) or not isinstance(candidate_services, dict):
        raise StageTransitionError("Graph bootstrap Compose service inventory is malformed")
    if "microsoft-graph" in active_services or "microsoft-graph" not in candidate_services:
        raise StageTransitionError("Graph bootstrap must add only the new Microsoft Graph service")

    _normalize_graph_cloud_memory_reallocation(active_services, candidate_services)
    _validate_office_graph_oauth_setup_delta(active_services, candidate_services)
    _validate_office_graph_personal_route_compose_delta(active_services, candidate_services)
    _validate_office_graph_private_principal_delta(active, candidate)

    for compose, services in ((active, active_services), (candidate, candidate_services)):
        for name in (
            "account-center",
            "exchange",
            "navigator",
            "navigator-persistence",
            "teams-provider",
            "teams-ingress",
        ):
            service = services.get(name)
            if not isinstance(service, dict):
                raise StageTransitionError(f"Graph bootstrap requires the existing {name} service")
            service.pop("image", None)
        services.pop("microsoft-graph", None)

    # The secret-file map is a closed string, so compare its parsed entries before removing it
    # from the structural diff. 中文：只允许增加对应 Graph HMAC key，不覆盖旧连接器映射。
    secret_map_additions = {
        "account-center": {
            account_map_key: "/run/secrets/account-center-graph-provider-hmac-key",
            account_callback_map_key: "/run/secrets/account-center-microsoft-graph-callback-hmac-key",
            account_client_map_key: "/run/secrets/account-center-microsoft-graph-client-secret",
        },
        "exchange": {
            exchange_map_key: "/run/secrets/exchange-graph-auth-context-hmac-key",
            exchange_callback_map_key: "/run/secrets/account-center-microsoft-graph-callback-hmac-key",
        },
    }
    secret_mount_additions = {
        "account-center": {graph_account_secret, graph_callback_secret, graph_client_secret},
        "exchange": {graph_exchange_secret, graph_callback_secret},
    }
    for service_name in ("account-center", "exchange"):
        active_service = active.get("services", {}).get(service_name, {})
        candidate_service = candidate.get("services", {}).get(service_name, {})
        active_map = preflight._parse_secret_file_map(active_service, service_name)
        candidate_map = preflight._parse_secret_file_map(candidate_service, service_name)
        if candidate_map != active_map | secret_map_additions[service_name]:
            raise StageTransitionError(
                f"Graph bootstrap changes an unrelated {service_name} secret mapping"
            )
        old_pairs = preflight._secret_mount_pairs(active_service, service_name)
        new_pairs = preflight._secret_mount_pairs(candidate_service, service_name)
        if new_pairs != old_pairs | secret_mount_additions[service_name]:
            raise StageTransitionError(
                f"Graph bootstrap changes an unrelated {service_name} secret mount"
            )

    active_account = active.get("services", {}).get("account-center", {})
    candidate_account = candidate.get("services", {}).get("account-center", {})
    old_account_config = preflight._json_environment_object(
        active_account, "CYRENE_CAPABILITY_CONFIGURATION_JSON", "Account Center"
    )
    new_account_config = preflight._json_environment_object(
        candidate_account, "CYRENE_CAPABILITY_CONFIGURATION_JSON", "Account Center"
    )
    _validate_office_graph_account_center_config_delta(old_account_config, new_account_config)

    active_exchange = active.get("services", {}).get("exchange", {})
    candidate_exchange = candidate.get("services", {}).get("exchange", {})
    old_integrations = preflight._json_environment_object(
        active_exchange, "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON", "Exchange"
    )
    new_integrations = preflight._json_environment_object(
        candidate_exchange, "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON", "Exchange"
    )
    old_authorization = old_integrations.get("authorizationCloud")
    new_authorization = new_integrations.get("authorizationCloud")
    old_profiles = (
        old_authorization.get("profiles") if isinstance(old_authorization, dict) else None
    )
    new_profiles = (
        new_authorization.get("profiles") if isinstance(new_authorization, dict) else None
    )
    if not isinstance(old_profiles, dict) or not isinstance(new_profiles, dict):
        raise StageTransitionError("Graph bootstrap Exchange profile map is malformed")
    new_graph_profiles = {
        alias: row
        for alias, row in new_profiles.items()
        if isinstance(row, dict) and row.get("provider") == "microsoft_graph"
    }
    if len(new_graph_profiles) != 1 or any(
        isinstance(row, dict) and row.get("provider") == "microsoft_graph"
        for row in old_profiles.values()
    ):
        raise StageTransitionError("Graph bootstrap must add exactly one unissued Exchange profile")
    normalized_integrations = copy.deepcopy(new_integrations)
    normalized_profiles = normalized_integrations["authorizationCloud"]["profiles"]
    for alias in new_graph_profiles:
        normalized_profiles.pop(alias)
    _validate_office_graph_exchange_config_delta(old_integrations, normalized_integrations)

    # Remove only the reviewed Graph additions and temporary UI route before comparing every
    # remaining Compose field byte-for-structure. 中文：端口只用于授权窗口，后续issued阶段会移除。
    for services in (active_services, candidate_services):
        services["account-center"]["environment"].pop("CYRENE_CAPABILITY_CONFIGURATION_JSON", None)
        for name in ("account-center", "exchange"):
            service = services[name]
            environment = service.get("environment", {})
            environment.pop("CYRENE_SECRET_FILE_MAP", None)
            service["environment"] = environment
            service["networks"] = sorted(preflight._network_names(service, name) - graph_networks)
            service["secrets"] = sorted(
                pair
                for pair in preflight._secret_mount_pairs(service, name)
                if pair
                not in {
                    graph_account_secret,
                    graph_callback_secret,
                    graph_client_secret,
                    graph_exchange_secret,
                }
                and pair[0] not in graph_secret_names
            )
        exchange = services["exchange"]
        dependencies = exchange.get("depends_on", {})
        if isinstance(dependencies, dict):
            dependencies.pop("microsoft-graph", None)
            exchange["depends_on"] = dependencies
        exchange.pop("ports", None)
        exchange["volumes"] = [
            row
            for row in exchange.get("volumes", [])
            if not (
                isinstance(row, dict) and row.get("target") == "/run/config/office-operator.json"
            )
        ]

    # The two integration documents were compared after removing the one Graph profile.
    candidate_services["exchange"]["environment"].pop(
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON", None
    )
    active_services["exchange"]["environment"].pop(
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON", None
    )

    active_normalized = copy.deepcopy(active)
    candidate_normalized = copy.deepcopy(candidate)
    active_normalized["services"] = active_services
    candidate_normalized["services"] = candidate_services
    candidate_normalized["networks"] = {
        name: row
        for name, row in candidate_normalized.get("networks", {}).items()
        if name not in graph_networks
    }
    candidate_normalized["secrets"] = {
        name: row
        for name, row in candidate_normalized.get("secrets", {}).items()
        if name not in graph_secret_names | OFFICE_PERSONAL_ROUTE_SECRET_NAMES
    }
    for compose in (active_normalized, candidate_normalized):
        configs = compose.get("configs", {})
        if isinstance(configs, dict):
            for row in configs.values():
                if isinstance(row, dict) and isinstance(row.get("file"), str):
                    row["file"] = Path(row["file"]).name
    if active_normalized != candidate_normalized:
        raise StageTransitionError(
            "Graph bootstrap changes Compose fields outside its closed overlay"
        )


def _validate_office_graph_oauth_setup_delta(
    active_services: dict[str, Any], candidate_services: dict[str, Any]
) -> None:
    """Require the temporary owner config mount to exist only in the Graph candidate."""

    active_exchange = active_services.get("exchange", {})
    candidate_exchange = candidate_services.get("exchange", {})
    active_rows = [
        row
        for row in active_exchange.get("volumes", [])
        if isinstance(row, dict) and row.get("target") == "/run/config/office-operator.json"
    ]
    candidate_rows = [
        row
        for row in candidate_exchange.get("volumes", [])
        if isinstance(row, dict) and row.get("target") == "/run/config/office-operator.json"
    ]
    if active_rows or len(candidate_rows) != 1:
        raise StageTransitionError(
            "Graph bootstrap must add one temporary owner-only Office operator config mount"
        )
    if (
        candidate_rows[0].get("type") != "bind"
        or candidate_rows[0].get("read_only") is not True
        or not preflight._private_bind_options_are_safe(candidate_rows[0])
        or not isinstance(candidate_rows[0].get("source"), str)
        or set(candidate_rows[0]) - {"type", "source", "target", "read_only", "bind"}
    ):
        raise StageTransitionError(
            "Graph bootstrap Office operator config mount is not read-only and exact"
        )


def _validate_office_graph_private_principal_delta(
    active: dict[str, Any], candidate: dict[str, Any]
) -> None:
    """Permit the exact six-principal/private-chat extension of the owner-only baseline.

    中文：保留唯一原 session principal，只追加canonical五writer、bridge与Teams策略。
    """

    def config_directory(compose: dict[str, Any], label: str) -> Path:
        navigator = compose.get("services", {}).get("navigator", {})
        volumes = navigator.get("volumes", []) if isinstance(navigator, dict) else []
        rows = [
            row
            for row in volumes
            if isinstance(row, dict) and row.get("target") == "/run/teams-authorization"
        ]
        if (
            len(rows) != 1
            or rows[0].get("type") != "bind"
            or rows[0].get("read_only") is not True
            or not isinstance(rows[0].get("source"), str)
            or not Path(rows[0]["source"]).is_absolute()
        ):
            raise StageTransitionError(f"Graph bootstrap {label} actor config bind is malformed")
        path = Path(rows[0]["source"])
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise StageTransitionError(
                f"Graph bootstrap {label} actor config directory is unavailable"
            ) from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or resolved != path:
            raise StageTransitionError(f"Graph bootstrap {label} actor config directory is unsafe")
        return path

    active_directory = config_directory(active, "active")
    candidate_directory = config_directory(candidate, "candidate")
    if active_directory == candidate_directory:
        raise StageTransitionError(
            "Graph bootstrap requires an isolated candidate actor config directory"
        )

    def directory_entries(directory: Path, label: str) -> dict[str, Path]:
        entries: dict[str, Path] = {}
        try:
            children = list(directory.iterdir())
        except OSError as error:
            raise StageTransitionError(
                f"Graph bootstrap {label} actor config inventory is unavailable"
            ) from error
        for child in children:
            if child.is_symlink() or not child.is_file() or child.name in entries:
                raise StageTransitionError(
                    f"Graph bootstrap {label} actor config inventory is unsafe"
                )
            entries[child.name] = child
        return entries

    active_files = directory_entries(active_directory, "active")
    candidate_files = directory_entries(candidate_directory, "candidate")
    allowed_new_files = {"work-actor-token-map.json", "automation-source-bindings.json"}
    if (
        not set(active_files) <= set(candidate_files)
        or set(candidate_files) - set(active_files) - allowed_new_files
        or "work-actor-token-map.json" not in candidate_files
        or "automation-source-bindings.json" not in candidate_files
    ):
        raise StageTransitionError(
            "Graph bootstrap may add only the Work actor map and empty automation source bindings"
        )
    for filename in set(active_files) & set(candidate_files) - {
        NAVIGATOR_PRINCIPAL_CONFIG_FILENAME,
        "work-actor-token-map.json",
        "automation-source-bindings.json",
    }:
        if _sha256_file(active_files[filename]) != _sha256_file(candidate_files[filename]):
            raise StageTransitionError(
                f"Graph bootstrap changes unrelated private config {filename}"
            )

    def principal_document(
        compose: dict[str, Any], directory: Path, label: str
    ) -> tuple[str, dict[str, Any]]:
        configs = compose.get("configs", {})
        config_name = None
        row = None
        for name, candidate_row in configs.items() if isinstance(configs, dict) else []:
            if not isinstance(candidate_row, dict):
                continue
            consumers = _recovered_principal_config_consumers(compose, name)
            if consumers == [("navigator-persistence", NAVIGATOR_PRINCIPAL_CONFIG_TARGET)]:
                if config_name is not None:
                    raise StageTransitionError(
                        "Graph bootstrap has ambiguous Navigator principal configs"
                    )
                config_name, row = name, candidate_row
        if not isinstance(row, dict) or not isinstance(row.get("file"), str):
            raise StageTransitionError(f"Graph bootstrap {label} principal config is missing")
        path = Path(row["file"])
        expected_path = directory / NAVIGATOR_PRINCIPAL_CONFIG_FILENAME
        if path.resolve(strict=True) != expected_path.resolve(strict=True):
            raise StageTransitionError(
                f"Graph bootstrap {label} principal config path is not canonical"
            )
        document = _read_json(expected_path, f"{label} Navigator principal config")
        return config_name, document

    active_name, old = principal_document(active, active_directory, "active")
    candidate_name, new = principal_document(candidate, candidate_directory, "candidate")
    if active_name != candidate_name:
        raise StageTransitionError("Graph bootstrap changes the Navigator principal config alias")
    services = candidate.get("services", {})
    navigator_environment = services.get("navigator", {}).get("environment", {})
    operator_environment = services.get("directory-operator", {}).get("environment", {})
    tenant = navigator_environment.get("TEAMS_TENANT_ID")
    workspace = navigator_environment.get("CYRENE_WORKSPACE_ID")
    organization = operator_environment.get("CYRENE_ORGANIZATION_ID")
    owner = operator_environment.get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID")
    bot_app_id = navigator_environment.get("TEAMS_BOT_APP_ID")
    if any(
        not preflight._uuid(value) for value in (owner, workspace, organization, tenant, bot_app_id)
    ):
        raise StageTransitionError(
            "Graph bootstrap owner, tenant, workspace, or bot identity is malformed"
        )
    policy = {
        "actor_subjects_file_env": "TEAMS_TASK_ACTOR_SUBJECTS_FILE",
        "workspace_id": workspace,
        "issuer": f"https://login.microsoftonline.com/{tenant}/v2.0",
        "tenant_id": tenant,
        "bot_app_id": bot_app_id,
    }

    actor_map = _read_json(
        candidate_directory / "work-actor-token-map.json", "Graph bootstrap Work actor map"
    )
    actor_projection = _read_json(
        candidate_directory / "teams-actor-subjects.json",
        "Graph bootstrap Directory subject projection",
    )
    source_bindings = _read_json(
        candidate_directory / "automation-source-bindings.json",
        "Graph bootstrap automation source bindings",
    )
    all_writer_slots = [f"CYRENE_WORK_ACTOR_TOKEN_{index:02d}" for index in range(1, 6)]
    actor_rows = actor_map.get("actors")
    if (
        set(actor_map) != {"schemaVersion", "workspaceId", "actors"}
        or actor_map.get("schemaVersion") != 1
        or actor_map.get("workspaceId") != workspace
        or not isinstance(actor_rows, list)
        or not 1 <= len(actor_rows) <= 5
        or [row.get("tokenEnv") for row in actor_rows if isinstance(row, dict)]
        != all_writer_slots[: len(actor_rows)]
        or any(
            not isinstance(row, dict)
            or set(row) != {"actorAadObjectId", "tokenEnv"}
            or not preflight._uuid(row.get("actorAadObjectId"))
            for row in actor_rows
        )
    ):
        raise StageTransitionError(
            "Graph bootstrap Work actor map does not match contiguous owner-first slots"
        )
    writer_slots = all_writer_slots[: len(actor_rows)]
    actor_by_token = {row["tokenEnv"]: row["actorAadObjectId"] for row in actor_rows}
    actor_ids = list(actor_by_token.values())
    if len(set(actor_ids)) != len(writer_slots) or actor_by_token.get(writer_slots[0]) != owner:
        raise StageTransitionError(
            "Graph bootstrap Work actor map has duplicate actors or a non-owner slot 01"
        )
    subjects = actor_projection.get("subjects")
    if (
        set(actor_projection) != {"schemaVersion", "workspaceId", "issuer", "subjects"}
        or actor_projection.get("schemaVersion") != 1
        or actor_projection.get("workspaceId") != workspace
        or actor_projection.get("issuer") != policy["issuer"]
        or not isinstance(subjects, list)
        or len(subjects) != len(writer_slots)
        or any(not preflight._uuid(subject) for subject in subjects)
        or len(set(subjects)) != len(writer_slots)
        or set(subjects) != set(actor_ids)
        or source_bindings != {"schemaVersion": 1, "targets": []}
    ):
        raise StageTransitionError(
            "Graph bootstrap Directory projection must match selected actors and source bindings must be empty"
        )
    connector_host = preflight._json_environment_object(
        services.get("exchange", {}),
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON",
        "Exchange integration",
    ).get("connectorHost")
    teams_profile = (
        connector_host.get("teamsMessages") if isinstance(connector_host, dict) else None
    )
    if (
        not isinstance(teams_profile, dict)
        or not isinstance(teams_profile.get("personalActorSubjects"), list)
        or len(teams_profile["personalActorSubjects"]) != len(writer_slots)
        or any(not preflight._uuid(subject) for subject in teams_profile["personalActorSubjects"])
        or len(set(teams_profile["personalActorSubjects"])) != len(writer_slots)
        or set(teams_profile["personalActorSubjects"]) != set(subjects)
    ):
        raise StageTransitionError(
            "Graph bootstrap personal chat subjects must match the selected Directory-projected actors"
        )

    old_rows = old.get("principals")
    new_rows = new.get("principals")
    expected_tokens = [
        "NAVIGATOR_SESSION_TOKEN",
        *writer_slots,
        "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN",
    ]
    if (
        not isinstance(old_rows, list)
        or len(old_rows) != 1
        or not isinstance(old_rows[0], dict)
        or old_rows[0].get("token_env") != "NAVIGATOR_SESSION_TOKEN"
        or old_rows[0].get("actor_id") != owner
        or not isinstance(new_rows, list)
        or [row.get("token_env") for row in new_rows if isinstance(row, dict)] != expected_tokens
        or any(not isinstance(row, dict) for row in new_rows)
        or new_rows[0] != old_rows[0]
        or "teams_private_conversations" in old
        or new.get("teams_private_conversations") != policy
    ):
        raise StageTransitionError(
            "Graph bootstrap must preserve the owner session and add only selected writers, bridge, and private-chat policy"
        )

    principal_fields = {
        "token_env",
        "actor_id",
        "workspace_ids",
        "organization_id",
        "can_takeover",
        "actor_private_scope",
        "can_write_harness",
        "can_publish_credential_health",
        "can_bind_teams_private_conversations",
    }
    expected_writer_flags = {
        "can_takeover": True,
        "actor_private_scope": True,
        "can_write_harness": True,
        "can_publish_credential_health": False,
        "can_bind_teams_private_conversations": False,
    }
    for token_env, principal in zip(writer_slots, new_rows[1:-1], strict=True):
        if (
            set(principal) != principal_fields
            or principal.get("actor_id") != actor_by_token[token_env]
            or principal.get("workspace_ids") != old_rows[0].get("workspace_ids")
            or principal.get("organization_id") != old_rows[0].get("organization_id")
            or any(
                principal.get(field) is not value for field, value in expected_writer_flags.items()
            )
        ):
            raise StageTransitionError(
                "Graph bootstrap writer principals exceed the canonical private-writer patch"
            )
    bridge = new_rows[-1]
    if (
        set(bridge) != principal_fields
        or bridge.get("actor_id") != "teams-private-bridge"
        or bridge.get("workspace_ids") != old_rows[0].get("workspace_ids")
        or bridge.get("organization_id") != old_rows[0].get("organization_id")
        or bridge.get("can_takeover") is not False
        or bridge.get("actor_private_scope") is not False
        or bridge.get("can_write_harness") is not False
        or bridge.get("can_publish_credential_health") is not False
        or bridge.get("can_bind_teams_private_conversations") is not True
    ):
        raise StageTransitionError(
            "Graph bootstrap Teams bridge principal exceeds its narrow bind-only role"
        )

    normalized = copy.deepcopy(new)
    normalized.pop("teams_private_conversations", None)
    normalized["principals"] = [new_rows[0]]
    if old != normalized:
        raise StageTransitionError(
            "Graph bootstrap changes existing owner principal fields beyond the private-chat route"
        )


def _validate_office_graph_personal_route_compose_delta(
    active_services: dict[str, Any], candidate_services: dict[str, Any]
) -> None:
    """Normalize only the existing five-actor personal route and owner-only model gate.

    中文：只接受既有个人会话 overlay 的精确开关、Work token 挂载和 owner 模型身份。
    """

    names = ("navigator", "navigator-persistence", "teams-ingress", "teams-provider")
    services = {
        label: (active_services.get(label), candidate_services.get(label)) for label in names
    }
    if any(
        not isinstance(old, dict) or not isinstance(new, dict) for old, new in services.values()
    ):
        raise StageTransitionError(
            "Graph bootstrap requires the existing five-actor Teams route services"
        )

    navigator_old, navigator_new = services["navigator"]
    persistence_old, persistence_new = services["navigator-persistence"]
    ingress_old, ingress_new = services["teams-ingress"]
    provider_old, provider_new = services["teams-provider"]
    owner_row = candidate_services.get("directory-operator", {})
    owner_environment = owner_row.get("environment", {}) if isinstance(owner_row, dict) else {}
    owner_id = owner_environment.get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID")
    if not preflight._uuid(owner_id):
        raise StageTransitionError(
            "Graph bootstrap owner model binding has no verified Directory operator"
        )

    def environment_pair(
        service_pair: tuple[dict[str, Any], dict[str, Any]], label: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        old_environment = service_pair[0].get("environment", {})
        new_environment = service_pair[1].get("environment", {})
        if not isinstance(old_environment, dict) or not isinstance(new_environment, dict):
            raise StageTransitionError(f"Graph bootstrap {label} environment is malformed")
        return old_environment, new_environment

    nav_old_env, nav_new_env = environment_pair((navigator_old, navigator_new), "Navigator")
    ingress_old_env, ingress_new_env = environment_pair((ingress_old, ingress_new), "Teams ingress")
    persistence_old_env, persistence_new_env = environment_pair(
        (persistence_old, persistence_new), "Navigator Persistence"
    )
    provider_old_env, provider_new_env = environment_pair(
        (provider_old, provider_new), "Teams provider"
    )

    nav_values = {
        "CYRENE_WORK_ACTOR_TOKEN_MAP_FILE": "/run/teams-authorization/work-actor-token-map.json",
        "CYRENE_AUTOMATION_SOURCE_BINDINGS_FILE": "/run/teams-authorization/automation-source-bindings.json",
        "CYRENE_WORKFLOWS_ENABLED": "false",
        "CYRENE_TEAMS_PERSONAL_CHAT_ENABLED": "true",
        "CYRENE_TEAMS_MODEL_ACTOR_SUBJECT": owner_id,
    }
    for key, expected in nav_values.items():
        if nav_new_env.get(key) != expected:
            if key == "CYRENE_TEAMS_MODEL_ACTOR_SUBJECT":
                raise StageTransitionError(
                    "Graph bootstrap model authorization must be bound to the verified owner"
                )
            if key == "CYRENE_WORKFLOWS_ENABLED":
                raise StageTransitionError("Graph bootstrap must keep Workflows disabled")
            raise StageTransitionError(
                f"Graph bootstrap Navigator personal route requires {key}={expected}"
            )
        previous = nav_old_env.get(key)
        if key in {"CYRENE_WORKFLOWS_ENABLED"} and previous not in {None, "false"}:
            raise StageTransitionError("Graph bootstrap must keep Navigator Workflows disabled")
        if key == "CYRENE_TEAMS_PERSONAL_CHAT_ENABLED" and previous not in {None, "false"}:
            raise StageTransitionError(
                "Graph bootstrap personal chat may only change from disabled to enabled"
            )
        if key == "CYRENE_TEAMS_MODEL_ACTOR_SUBJECT" and previous not in {None, "", owner_id}:
            raise StageTransitionError(
                "Graph bootstrap cannot transfer model access from another actor"
            )
        if key not in {
            "CYRENE_WORKFLOWS_ENABLED",
            "CYRENE_TEAMS_PERSONAL_CHAT_ENABLED",
            "CYRENE_TEAMS_MODEL_ACTOR_SUBJECT",
        } and previous not in {None, expected}:
            raise StageTransitionError("Graph bootstrap changes an existing Navigator route path")

    route_secret_map = {
        "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN": "/run/secrets/teams-private-bridge-token",
        **{
            f"CYRENE_WORK_ACTOR_TOKEN_{index:02d}": f"/run/secrets/work-actor-token-{index:02d}"
            for index in range(1, 6)
        },
    }
    for label, old_service, new_service in (
        ("Navigator", navigator_old, navigator_new),
        ("Navigator Persistence", persistence_old, persistence_new),
    ):
        old_map = preflight._parse_secret_file_map(old_service, label)
        new_map = preflight._parse_secret_file_map(new_service, label)
        if new_map != old_map | route_secret_map:
            raise StageTransitionError(f"Graph bootstrap changes unrelated {label} secret mappings")
        old_pairs = preflight._secret_mount_pairs(old_service, label)
        new_pairs = preflight._secret_mount_pairs(new_service, label)
        expected_pairs = {(name, name) for name in OFFICE_PERSONAL_ROUTE_SECRET_NAMES}
        if new_pairs != old_pairs | expected_pairs:
            raise StageTransitionError(f"Graph bootstrap changes unrelated {label} secret mounts")
        new_service["secrets"] = [
            row
            for row in new_service.get("secrets", [])
            if not (
                isinstance(row, dict) and (row.get("source"), row.get("target")) in expected_pairs
            )
        ]
        new_service.setdefault("environment", {}).pop("CYRENE_SECRET_FILE_MAP", None)
        old_service.setdefault("environment", {}).pop("CYRENE_SECRET_FILE_MAP", None)

    if persistence_new_env.get(
        "TEAMS_TASK_ACTOR_SUBJECTS_FILE"
    ) != "/run/teams-authorization/teams-actor-subjects.json" or persistence_old_env.get(
        "TEAMS_TASK_ACTOR_SUBJECTS_FILE"
    ) not in {None, "/run/teams-authorization/teams-actor-subjects.json"}:
        raise StageTransitionError(
            "Graph bootstrap Persistence must use the existing actor projection"
        )
    for key, expected in nav_values.items():
        nav_new_env.pop(key, None)
        nav_old_env.pop(key, None)
    for env in (persistence_old_env, persistence_new_env):
        env.pop("TEAMS_TASK_ACTOR_SUBJECTS_FILE", None)

    if ingress_new_env.get("TEAMS_PERSONAL_CHAT_ENABLED") != "true" or ingress_old_env.get(
        "TEAMS_PERSONAL_CHAT_ENABLED"
    ) not in {None, "false"}:
        raise StageTransitionError(
            "Graph bootstrap Teams ingress must enable only the personal chat route"
        )
    ingress_old_env.pop("TEAMS_PERSONAL_CHAT_ENABLED", None)
    ingress_new_env.pop("TEAMS_PERSONAL_CHAT_ENABLED", None)

    def provider_configuration(environment: dict[str, Any], label: str) -> dict[str, Any]:
        try:
            document = json.loads(
                environment.get("CYRENE_CAPABILITY_CONFIGURATION_JSON", ""),
                object_pairs_hook=preflight._unique_json_object,
            )
        except (TypeError, json.JSONDecodeError, ValueError) as error:
            raise StageTransitionError(
                f"Graph bootstrap {label} configuration is malformed"
            ) from error
        if not isinstance(document, dict):
            raise StageTransitionError(f"Graph bootstrap {label} configuration is malformed")
        return document

    old_provider = provider_configuration(provider_old_env, "active Teams provider")
    new_provider = provider_configuration(provider_new_env, "candidate Teams provider")
    if old_provider.get("personal_chat_enabled", False) is not False:
        raise StageTransitionError("Graph bootstrap must begin with Teams personal chat disabled")
    if new_provider.get("personal_chat_enabled") is not True:
        raise StageTransitionError(
            "Graph bootstrap must enable the Teams provider personal handler"
        )
    old_provider.pop("personal_chat_enabled", None)
    new_provider.pop("personal_chat_enabled", None)
    if old_provider != new_provider:
        raise StageTransitionError(
            "Graph bootstrap changes Teams provider fields beyond its personal handler"
        )
    provider_new_env["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(
        new_provider, sort_keys=True, separators=(",", ":")
    )
    provider_old_env["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(
        old_provider, sort_keys=True, separators=(",", ":")
    )

    def remove_overlay_mounts(
        service: dict[str, Any], targets: set[str], *, required: set[str], label: str
    ) -> None:
        rows = _mount_rows(service, "volumes")
        selected = [row for row in rows if row.get("target") in targets]
        selected_targets = {row.get("target") for row in selected}
        if not required <= selected_targets or len(selected) > len(targets):
            raise StageTransitionError(
                f"Graph bootstrap {label} is missing its private config mount"
            )
        for row in selected:
            if (
                row.get("type") != "bind"
                or row.get("read_only") is not True
                or not preflight._private_bind_options_are_safe(row)
                or not isinstance(row.get("source"), str)
                or not Path(row["source"]).is_absolute()
            ):
                raise StageTransitionError(
                    f"Graph bootstrap {label} mount is not a fixed read-only bind"
                )
        service["volumes"] = [row for row in rows if row.get("target") not in targets]

    try:
        if _volume_source_for_target(
            navigator_new, "/run/teams-authorization", kind="bind"
        ) != _volume_source_for_target(persistence_new, "/run/teams-authorization", kind="bind"):
            raise StageTransitionError(
                "Graph bootstrap Navigator and Persistence must share one actor projection directory"
            )
    except StageTransitionError as error:
        if "share one actor projection" in str(error):
            raise
        raise StageTransitionError(
            "Graph bootstrap is missing the private actor projection bind"
        ) from error

    remove_overlay_mounts(
        persistence_new,
        {"/run/teams-authorization"},
        required={"/run/teams-authorization"},
        label="Persistence actor projection",
    )
    remove_overlay_mounts(
        persistence_old,
        {"/run/teams-authorization"},
        required=set(),
        label="active Persistence actor projection",
    )
    remove_overlay_mounts(
        navigator_new,
        {"/run/cyrene/automation-health"},
        required={"/run/cyrene/automation-health"},
        label="Navigator sanitized health",
    )
    remove_overlay_mounts(
        navigator_old,
        {"/run/cyrene/automation-health"},
        required=set(),
        label="active Navigator sanitized health",
    )
    # The ordinary Navigator config bind follows the versioned private candidate config directory.
    for service in (navigator_old, navigator_new):
        matches = [
            row
            for row in _mount_rows(service, "volumes")
            if row.get("target") == "/run/teams-authorization"
        ]
        if (
            len(matches) != 1
            or matches[0].get("type") != "bind"
            or matches[0].get("read_only") is not True
        ):
            raise StageTransitionError(
                "Graph bootstrap must preserve Navigator's private actor config bind"
            )
        matches[0]["source"] = "${CYRENE_CONFIG_DIR}"
    operator_old = active_services.get("directory-operator")
    operator_new = candidate_services.get("directory-operator")
    if isinstance(operator_old, dict) and isinstance(operator_new, dict):
        for service in (operator_old, operator_new):
            matches = [
                row
                for row in _mount_rows(service, "volumes")
                if row.get("target") == "/run/cyrene-config"
            ]
            if (
                len(matches) != 1
                or matches[0].get("type") != "bind"
                or not isinstance(matches[0].get("source"), str)
                or not Path(matches[0]["source"]).is_absolute()
            ):
                raise StageTransitionError(
                    "Graph bootstrap Directory operator must retain its private config bind"
                )
            matches[0]["source"] = "${CYRENE_CONFIG_DIR}"
    for name in names:
        old_service = copy.deepcopy(active_services[name])
        new_service = copy.deepcopy(candidate_services[name])
        old_service.pop("image", None)
        new_service.pop("image", None)
        if old_service != new_service:
            raise StageTransitionError(
                f"Graph bootstrap changes {name} beyond its pinned image and personal route"
            )


def _validate_office_graph_account_center_config_delta(
    active: dict[str, Any], candidate: dict[str, Any]
) -> None:
    """Permit only the Graph OAuth client and its dedicated Account Center callers."""

    graph_top_level_fields = {
        "microsoft_graph_client_id",
        "microsoft_graph_client_secret_ref",
        "microsoft_graph_callback_uri",
        "microsoft_graph_callback_caller_id",
        "microsoft_graph_provider_caller_id",
    }
    graph_host_refs = {
        "microsoft-graph/client-secret",
        "account-center/caller-hmac-keys/microsoft-graph-callback",
        "account-center/caller-hmac-keys/microsoft-graph-provider",
    }
    graph_callers = {"microsoft-entra-callback", "microsoft-graph-provider"}
    graph_methods = {
        "begin_microsoft_graph_link",
        "complete_microsoft_graph_link",
        "begin_microsoft_graph_scope_upgrade",
        "complete_microsoft_graph_scope_upgrade",
        "update_microsoft_graph_v2_scope",
    }

    old_methods = active.get("method_callers", {})
    new_methods = candidate.get("method_callers", {})
    if not isinstance(old_methods, dict) or not isinstance(new_methods, dict):
        raise StageTransitionError("Account Center method caller maps are malformed")
    exchange_caller = candidate.get("exchange_caller_id")
    callback_caller = candidate.get("microsoft_graph_callback_caller_id")
    provider_caller = candidate.get("microsoft_graph_provider_caller_id")

    def require_callers(method: str, expected: set[str]) -> list[str]:
        callers = new_methods.get(method)
        if (
            not isinstance(callers, list)
            or any(not isinstance(caller, str) for caller in callers)
            or len(callers) != len(set(callers))
            or set(callers) != expected
        ):
            raise StageTransitionError(
                f"Account Center Graph method callers are not exact for {method}"
            )
        return callers

    for method, expected_callers in {
        "begin_microsoft_graph_link": {exchange_caller},
        "complete_microsoft_graph_link": {callback_caller},
        "begin_microsoft_graph_scope_upgrade": {exchange_caller},
        "complete_microsoft_graph_scope_upgrade": {callback_caller},
        "update_microsoft_graph_v2_scope": {exchange_caller},
    }.items():
        if method in new_methods:
            require_callers(method, expected_callers)
    if "list_provider_profiles" in new_methods:
        callers = require_callers("list_provider_profiles", {exchange_caller, provider_caller})
        old_callers = old_methods.get("list_provider_profiles")
        if old_callers is not None and (
            not isinstance(old_callers, list)
            or callers
            != [*old_callers, *([] if provider_caller in old_callers else [provider_caller])]
        ):
            raise StageTransitionError(
                "Account Center Graph profile listing may add only its provider caller"
            )
    old_redeem_callers = old_methods.get("redeem_provider_credential_lease")
    if (
        not isinstance(old_redeem_callers, list)
        or any(not isinstance(caller, str) for caller in old_redeem_callers)
        or set(old_redeem_callers) != {"github-issues-service", "huggingface-provider"}
        or len(old_redeem_callers) != 2
    ):
        raise StageTransitionError(
            "Account Center credential lease roles differ from the live GitHub/HF callers"
        )
    require_callers(
        "redeem_provider_credential_lease",
        {*old_redeem_callers, provider_caller},
    )
    if new_methods["redeem_provider_credential_lease"] != [
        *old_redeem_callers,
        provider_caller,
    ]:
        raise StageTransitionError(
            "Graph provider must be appended to the existing lease redemption roles"
        )

    def normalized(value: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(value)
        for field in graph_top_level_fields:
            result.pop(field, None)
        for field in ("hostSecretRefs", "caller_key_refs"):
            mapping = result.get(field)
            if isinstance(mapping, dict):
                for key in graph_host_refs if field == "hostSecretRefs" else graph_callers:
                    mapping.pop(key, None)
        methods = result.get("method_callers")
        if isinstance(methods, dict):
            for method in graph_methods:
                methods.pop(method, None)
            for method in ("list_provider_profiles", "redeem_provider_credential_lease"):
                if method in old_methods:
                    methods[method] = copy.deepcopy(old_methods[method])
                else:
                    methods.pop(method, None)
        return result

    if normalized(active) != normalized(candidate):
        raise StageTransitionError(
            "Graph bootstrap changes Account Center configuration outside the OAuth caller and secret refs"
        )


def _normalize_graph_cloud_memory_reallocation(
    active_services: dict[str, Any], candidate_services: dict[str, Any]
) -> None:
    """Prove the sole non-Graph runtime delta is Cloud memory 384 MiB to 256 MiB."""

    name = "cloud-management-phase2"
    active = active_services.get(name)
    candidate = candidate_services.get(name)
    if not isinstance(active, dict) or not isinstance(candidate, dict):
        raise StageTransitionError("Graph bootstrap requires the existing Cloud Management service")
    for field in ("mem_limit", "memswap_limit"):
        active_bytes = preflight._memory_limit_bytes(active.get(field))
        candidate_bytes = preflight._memory_limit_bytes(candidate.get(field))
        if active_bytes != 384 * preflight.MIB or candidate_bytes != 256 * preflight.MIB:
            raise StageTransitionError(
                "Graph bootstrap must reallocate Cloud memory exactly from 384 MiB to 256 MiB"
            )
    if active.get("cpus") != candidate.get("cpus"):
        raise StageTransitionError("Graph bootstrap must preserve the Cloud CPU limit")

    active_normalized = copy.deepcopy(active)
    candidate_normalized = copy.deepcopy(candidate)
    for field in ("mem_limit", "memswap_limit"):
        candidate_normalized[field] = active_normalized[field]

    def deploy_memory(service: dict[str, Any]) -> tuple[dict[str, Any] | None, int | None]:
        deploy = service.get("deploy")
        if deploy is None:
            return None, None
        if not isinstance(deploy, dict):
            raise StageTransitionError("Cloud deploy resources are malformed")
        resources = deploy.get("resources")
        if resources is None:
            return None, None
        if not isinstance(resources, dict):
            raise StageTransitionError("Cloud deploy resources are malformed")
        limits = resources.get("limits")
        if limits is None:
            return None, None
        if not isinstance(limits, dict):
            raise StageTransitionError("Cloud deploy memory limit is malformed")
        return limits, preflight._memory_limit_bytes(limits.get("memory"))

    active_limits, active_deploy_memory = deploy_memory(active)
    candidate_limits, candidate_deploy_memory = deploy_memory(candidate)
    if (active_limits is None) != (candidate_limits is None):
        raise StageTransitionError("Graph bootstrap must preserve Cloud deploy resource shape")
    if active_limits is not None and candidate_limits is not None:
        if (
            active_deploy_memory != 384 * preflight.MIB
            or candidate_deploy_memory != 256 * preflight.MIB
        ):
            raise StageTransitionError(
                "Graph bootstrap must reallocate Cloud deploy memory exactly from 384 MiB to 256 MiB"
            )
        candidate_normalized["deploy"]["resources"]["limits"]["memory"] = active_normalized[
            "deploy"
        ]["resources"]["limits"]["memory"]
    if active_normalized != candidate_normalized:
        raise StageTransitionError(
            "Graph bootstrap Cloud configuration changes fields beyond the memory limits"
        )
    candidate_services[name] = candidate_normalized


def _validate_office_graph_exchange_config_delta(
    active: dict[str, Any], candidate: dict[str, Any]
) -> None:
    """Allow Graph OAuth setup and the existing five-actor private chat route."""

    account_center = candidate.get("accountCenter")
    refs = candidate.get("hostSecretRefs")
    if (
        not isinstance(account_center, dict)
        or account_center.get("callbackCallerId") != "microsoft-entra-callback"
        or account_center.get("callbackSigningKeyRef")
        != "account-center/caller-hmac-keys/microsoft-graph-callback"
        or not isinstance(refs, dict)
        or refs.get("account-center/caller-hmac-keys/microsoft-graph-callback")
        != "CYRENE_ACCOUNT_CENTER_MICROSOFT_GRAPH_CALLBACK_HMAC_KEY"
    ):
        raise StageTransitionError("Graph bootstrap lacks the fixed Exchange OAuth callback signer")
    graph_exchange_ref = "microsoft-graph/exchange-verification-key"
    if (
        not isinstance(refs, dict)
        or refs.get(graph_exchange_ref) != "EXCHANGE_GRAPH_AUTH_CONTEXT_HMAC_KEY"
    ):
        raise StageTransitionError(
            "Graph bootstrap lacks the fixed Exchange Graph verification HMAC ref"
        )
    normalized = copy.deepcopy(candidate)
    normalized_active = copy.deepcopy(active)
    normalized_account_center = normalized.get("accountCenter")
    if isinstance(normalized_account_center, dict):
        normalized_account_center.pop("callbackCallerId", None)
        normalized_account_center.pop("callbackSigningKeyRef", None)
    normalized_refs = normalized.get("hostSecretRefs")
    if isinstance(normalized_refs, dict):
        normalized_refs.pop("account-center/caller-hmac-keys/microsoft-graph-callback", None)
        normalized_refs.pop(graph_exchange_ref, None)
    for label, document in (("active", normalized_active), ("candidate", normalized)):
        connector_host = document.get("connectorHost")
        teams_profile = (
            connector_host.get("teamsMessages") if isinstance(connector_host, dict) else None
        )
        if not isinstance(teams_profile, dict):
            raise StageTransitionError(
                f"Graph bootstrap {label} Teams message profile is malformed"
            )
        if label == "candidate":
            subjects = teams_profile.get("personalActorSubjects")
            if (
                not isinstance(subjects, list)
                or not 1 <= len(subjects) <= 5
                or any(not preflight._uuid(subject) for subject in subjects)
                or len(set(subjects)) != len(subjects)
            ):
                raise StageTransitionError(
                    "Graph bootstrap Teams private route must use one to five valid actor subjects"
                )
        teams_profile.pop("personalActorSubjects", None)
    if normalized != normalized_active:
        raise StageTransitionError(
            "Graph bootstrap changes unrelated Exchange profile or Account Center configuration"
        )


def _memory_update_limit_bytes(service: dict[str, Any], label: str) -> int:
    """Read finite no-swap memory limits, including an optional deploy limit.

    中文：Compose 资源字段必须明确有限，且 swap 字节数等于 memory 字节数。
    """

    memory_bytes = preflight._memory_limit_bytes(service.get("mem_limit"))
    memory_swap_bytes = preflight._memory_limit_bytes(service.get("memswap_limit"))
    if memory_bytes is None or memory_swap_bytes is None:
        raise StageTransitionError(f"{label} must declare finite mem_limit and memswap_limit")
    if memory_swap_bytes != memory_bytes:
        raise StageTransitionError(f"{label} must preserve the existing no-swap memory policy")

    deploy = service.get("deploy")
    if deploy is not None and not isinstance(deploy, dict):
        raise StageTransitionError(f"{label} deploy configuration is malformed")
    resources = deploy.get("resources") if isinstance(deploy, dict) else None
    if resources is not None and not isinstance(resources, dict):
        raise StageTransitionError(f"{label} deploy.resources is malformed")
    limits = resources.get("limits") if isinstance(resources, dict) else None
    if limits is not None and not isinstance(limits, dict):
        raise StageTransitionError(f"{label} deploy.resources.limits is malformed")
    if isinstance(limits, dict) and "memory" in limits:
        deploy_memory_bytes = preflight._memory_limit_bytes(limits.get("memory"))
        if deploy_memory_bytes != memory_bytes:
            raise StageTransitionError(
                f"{label} deploy.resources.limits.memory must match mem_limit"
            )
    return memory_bytes


def _compose_without_service_memory_limits(compose: dict[str, Any], target: str) -> dict[str, Any]:
    """Copy Compose while removing only the target's supported memory fields."""

    result = copy.deepcopy(compose)
    services = result.get("services")
    service = services.get(target) if isinstance(services, dict) else None
    if not isinstance(service, dict):
        raise StageTransitionError(f"memory update target {target} has no Compose service")
    service.pop("mem_limit", None)
    service.pop("memswap_limit", None)
    deploy = service.get("deploy")
    resources = deploy.get("resources") if isinstance(deploy, dict) else None
    limits = resources.get("limits") if isinstance(resources, dict) else None
    if isinstance(limits, dict):
        limits.pop("memory", None)
    return result


def _validate_memory_update_topology(
    *,
    phase: str,
    target: str,
    active: dict[str, Any],
    candidate: dict[str, Any],
    active_lock: dict[str, Any],
    candidate_lock: dict[str, Any],
    old_services: dict[str, dict[str, Any]],
    new_services: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Allow one existing service's finite no-swap memory limit to increase in place."""

    if phase != "issued-connector-activation":
        raise StageTransitionError("memory updates require issued-connector-activation")
    if not isinstance(target, str) or target not in MEMORY_UPDATE_TARGETS:
        raise StageTransitionError("memory update target must be one existing runtime service")
    if _runtime_service_shape(old_services, "active memory update") != _runtime_service_shape(
        new_services, "candidate memory update"
    ):
        raise StageTransitionError(
            "memory update must preserve its exact 14/15/16-service topology"
        )
    if target not in old_services or target not in new_services:
        raise StageTransitionError("memory update target is not active in the selected runtime")
    if active_lock != candidate_lock:
        raise StageTransitionError("memory update must preserve the exact image lock")

    active_service = old_services[target]
    candidate_service = new_services[target]
    active_memory = _memory_update_limit_bytes(active_service, f"active {target} service")
    candidate_memory = _memory_update_limit_bytes(candidate_service, f"candidate {target} service")
    if candidate_memory <= active_memory:
        raise StageTransitionError("memory updates may only increase the selected service limit")
    if _compose_without_service_memory_limits(
        active, target
    ) != _compose_without_service_memory_limits(candidate, target):
        raise StageTransitionError(
            "memory update may change only the selected service memory limit fields"
        )

    target_images: dict[str, dict[str, Any]] = {}
    active_ids = _lock_image_ids(active_lock)
    candidate_ids = _lock_image_ids(candidate_lock)
    for service_name in _runtime_service_order_for(new_services):
        active_image = _service_image(active, active_ids, service_name)
        candidate_image = _service_image(candidate, candidate_ids, service_name)
        if active_image != candidate_image:
            raise StageTransitionError("memory update must preserve every service image ID")
        if _mounted_file_digests(active, service_name) != _mounted_file_digests(
            candidate, service_name
        ):
            raise StageTransitionError(
                f"memory update changes mounted configuration for {service_name}"
            )
        target_images[service_name] = {
            "active": active_image,
            "candidate": candidate_image,
            "added": False,
            "configChanged": service_name == target,
        }
    target_healthcheck = candidate_service.get("healthcheck")
    if not isinstance(target_healthcheck, dict):
        raise StageTransitionError("memory update target must expose a health check")
    return [target], target_images


def _memory_update_container_snapshot(
    *,
    container_id: str,
    project: str,
    service_name: str,
    expected_image: dict[str, str],
    expected_mounts: list[dict[str, Any]],
    expected_network_names: list[str],
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Read one healthy container and fingerprint its immutable Docker configuration."""

    if FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError("memory update container ID is malformed")
    rows = runtime_update._decode_json_output(
        runtime_update._checked_stdout(
            [*docker_prefix, "inspect", container_id],
            label=f"{service_name} memory update identity",
        ),
        f"{service_name} memory update identity",
    )
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise StageTransitionError("memory update container inspection is ambiguous")
    row = rows[0]
    config = row.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    host_config = row.get("HostConfig")
    state = row.get("State")
    image_ref = config.get("Image") if isinstance(config, dict) else None
    image_id = row.get("Image")
    compose_config_hash = (
        labels.get("com.docker.compose.config-hash") if isinstance(labels, dict) else None
    )
    memory_bytes = host_config.get("Memory") if isinstance(host_config, dict) else None
    memory_swap_bytes = host_config.get("MemorySwap") if isinstance(host_config, dict) else None
    process_id = state.get("Pid") if isinstance(state, dict) else None
    process_started_at = state.get("StartedAt") if isinstance(state, dict) else None
    restart_count = row.get("RestartCount")
    if (
        not isinstance(config, dict)
        or not isinstance(labels, dict)
        or labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != service_name
        or not isinstance(compose_config_hash, str)
        or SHA256_RE.fullmatch(compose_config_hash) is None
        or image_ref != expected_image.get("imageRef")
        or image_id != expected_image.get("imageId")
        or not isinstance(host_config, dict)
        or isinstance(memory_bytes, bool)
        or not isinstance(memory_bytes, int)
        or memory_bytes <= 0
        or isinstance(memory_swap_bytes, bool)
        or not isinstance(memory_swap_bytes, int)
        or memory_swap_bytes <= 0
        or not isinstance(state, dict)
        or state.get("Running") is not True
        or state.get("Paused") is True
        or isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id <= 0
        or not isinstance(process_started_at, str)
        or not process_started_at.strip()
        or isinstance(restart_count, bool)
        or not isinstance(restart_count, int)
        or restart_count < 0
        or not isinstance(state.get("Health"), dict)
        or state["Health"].get("Status") != "healthy"
    ):
        raise StageTransitionError(
            f"{service_name} container identity, no-swap limits, or health is not exact"
        )
    actual_id = row.get("Id")
    if actual_id != container_id:
        raise StageTransitionError(f"{service_name} container ID changed during inspection")
    networks_record = row.get("NetworkSettings")
    networks = networks_record.get("Networks") if isinstance(networks_record, dict) else None
    mounts = row.get("Mounts")
    if (
        not isinstance(networks, dict)
        or sorted(networks) != expected_network_names
        or not _service_mounts_match(mounts, expected_mounts)
    ):
        raise StageTransitionError(f"{service_name} network or mount identity changed")
    host_config_without_memory = dict(host_config)
    host_config_without_memory.pop("Memory", None)
    host_config_without_memory.pop("MemorySwap", None)
    return {
        "containerId": actual_id,
        "imageRef": image_ref,
        "imageId": image_id,
        "memoryBytes": memory_bytes,
        "memorySwapBytes": memory_swap_bytes,
        "processId": process_id,
        "processStartedAt": process_started_at,
        "restartCount": restart_count,
        "health": state["Health"]["Status"],
        "composeConfigHash": compose_config_hash,
        "containerConfigSha256": _sha256_bytes(_canonical_json(config)),
        "hostConfigSha256WithoutMemory": _sha256_bytes(_canonical_json(host_config_without_memory)),
    }


def _compose_service_config_hash(
    *,
    service_name: str,
    compose_files: list[Path],
    env_files: list[Path],
    docker_prefix: list[str],
) -> str:
    """Return Docker Compose's own hash for the exact selected service definition."""

    argv = [*docker_prefix, "compose"]
    for env_file in env_files:
        argv.extend(["--env-file", str(env_file.resolve())])
    for compose_file in compose_files:
        argv.extend(["--file", str(compose_file.resolve())])
    for profile in preflight.ACTIVE_COMPOSE_PROFILES:
        argv.extend(["--profile", profile])
    argv.extend(["config", "--hash", service_name])
    output = runtime_update._checked_stdout(
        argv, label=f"{service_name} Compose config hash", timeout_seconds=30
    )
    rows = output.splitlines()
    fields = rows[0].split() if len(rows) == 1 else []
    if len(fields) != 2 or fields[0] != service_name or SHA256_RE.fullmatch(fields[1]) is None:
        raise StageTransitionError(f"{service_name} Compose config hash is malformed")
    return fields[1]


def _prior_memory_update_hash_proof(
    *,
    selection_record: dict[str, Any],
    selection_path: str,
    journal_dir: Path,
    target: str,
    active_compose_hash: str,
    container_compose_hash: str,
    active_service: dict[str, Any],
) -> dict[str, str] | None:
    """Follow selector-linked journals to prove this container's intentional stale hash."""

    current = selection_record.get("document")
    expected_digest = selection_record.get("sha256")
    for _ in range(64):
        if not isinstance(current, dict) or not isinstance(current.get("operationId"), str):
            return None
        try:
            journal = _read_journal(_journal_path(journal_dir, current["operationId"]))
            if (
                journal.get("state") != "APPLIED"
                or journal.get("selection", {}).get("promoted") is not True
                or journal.get("selection", {}).get("path") != selection_path
                or journal.get("selection", {}).get("newSha256") != expected_digest
                or _sha256_bytes(_canonical_json(current) + b"\n") != expected_digest
            ):
                return None
            plan_path = Path(journal["planPath"])
            plan_metadata = plan_path.lstat()
            if (
                not preflight._private_artifact(plan_path)
                or plan_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(plan_metadata.st_mode) != 0o600
            ):
                return None
            plan = _read_json(plan_path, "selector-linked transition plan")
            _validate_plan(plan)
        except (OSError, StageTransitionError):
            return None
        if plan.get("operationId") != current.get("operationId") or plan.get(
            "planSha256"
        ) != journal.get("planSha256"):
            return None
        if plan.get("candidateSelectionFile") != selection_path:
            return None

        prior_update = journal.get("memoryUpdate")
        prior_state = plan.get("memoryUpdateState")
        prior_service = plan.get("activeServices", {}).get(target)
        prior_after = prior_update.get("afterReadback") if isinstance(prior_update, dict) else None
        if (
            journal.get("state") == "APPLIED"
            and isinstance(prior_update, dict)
            and prior_update.get("required") is True
            and prior_update.get("target") == target
            and prior_update.get("outcome") == "APPLIED_SAME_CONTAINER"
            and isinstance(prior_state, dict)
            and prior_state.get("candidateComposeConfigHash") == active_compose_hash
            and prior_state.get("containerComposeConfigHash") == container_compose_hash
            and isinstance(prior_service, dict)
            and prior_service.get("containerId") == active_service.get("containerId")
            and prior_service.get("imageId") == active_service.get("imageId")
            and isinstance(prior_after, dict)
            and prior_after.get("containerId") == active_service.get("containerId")
            and prior_after.get("composeConfigHash") == container_compose_hash
        ):
            return {"operationId": plan["operationId"], "planSha256": plan["planSha256"]}

        # A prior completed update proves selector/hash lineage only; the current plan pins
        # the current process independently, so a restart between completed updates is valid.
        previous = plan.get("activeSelection")
        previous_document = previous.get("document") if isinstance(previous, dict) else None
        previous_digest = previous.get("sha256") if isinstance(previous, dict) else None
        if (
            not isinstance(previous_document, dict)
            or not isinstance(previous_digest, str)
            or _sha256_bytes(_canonical_json(previous_document) + b"\n") != previous_digest
        ):
            return None
        current = previous_document
        expected_digest = previous_digest
    return None


def _verify_memory_update_config_hash_relation(
    *, plan: dict[str, Any], snapshot: dict[str, Any]
) -> None:
    """Reconfirm active or selector-journal-proven stale Compose hash before mutation."""

    target = plan["memoryUpdateTarget"]
    state = plan["memoryUpdateState"]
    active_hash = state["activeComposeConfigHash"]
    actual_hash = snapshot.get("composeConfigHash")
    if actual_hash != state["containerComposeConfigHash"]:
        raise StageTransitionError("live Compose config-hash changed after memory update planning")
    if state["configHashRelation"] == "MATCHES_ACTIVE":
        if actual_hash != active_hash:
            raise StageTransitionError(
                "live Compose config-hash no longer matches the active selector"
            )
        return
    active_service = plan["activeServices"][target]
    proof = _prior_memory_update_hash_proof(
        selection_record=plan["activeSelection"],
        selection_path=plan["candidateSelectionFile"],
        journal_dir=Path(plan["journalDir"]),
        target=target,
        active_compose_hash=active_hash,
        container_compose_hash=actual_hash,
        active_service=active_service,
    )
    if proof != {
        "operationId": state["configHashProofOperationId"],
        "planSha256": state["configHashProofPlanSha256"],
    }:
        raise StageTransitionError("stale Compose config-hash lost its selector-journal proof")


def _memory_update_actual_capacity(
    *,
    compose: dict[str, Any],
    services: dict[str, dict[str, Any]],
    max_memory_mib: int,
    max_cpus: float,
    docker_prefix: list[str],
) -> dict[str, int]:
    """Verify actual memory and CPU caps for every active service."""

    expected = _runtime_service_inventory(compose)
    expected_names = _runtime_service_shape(expected, "memory capacity Compose")
    if expected_names != _runtime_service_shape(services, "memory capacity read-back"):
        raise StageTransitionError(
            "memory capacity read-back service inventory differs from Compose"
        )
    service_order = _runtime_service_order_for(expected)
    container_ids = [services[name].get("containerId") for name in service_order]
    if any(
        not isinstance(item, str) or FULL_CONTAINER_ID_RE.fullmatch(item) is None
        for item in container_ids
    ):
        raise StageTransitionError("memory capacity read-back has a malformed container ID")
    rows = runtime_update._decode_json_output(
        runtime_update._checked_stdout(
            [*docker_prefix, "inspect", *container_ids], label="14-service memory limit read-back"
        ),
        "14-service memory limit read-back",
    )
    if not isinstance(rows, list) or len(rows) != len(service_order):
        raise StageTransitionError("Docker returned an incomplete runtime memory inventory")
    total_bytes = 0
    total_nano_cpus = 0
    seen: set[str] = set()
    for row in rows:
        config = row.get("Config") if isinstance(row, dict) else None
        labels = config.get("Labels") if isinstance(config, dict) else None
        name = labels.get("com.docker.compose.service") if isinstance(labels, dict) else None
        host_config = row.get("HostConfig") if isinstance(row, dict) else None
        if not isinstance(name, str) or name not in expected:
            raise StageTransitionError("memory capacity read-back contains an unknown service")
        expected_memory = _memory_update_limit_bytes(expected[name], f"selected {name} service")
        expected_cpus = preflight._cpu_limit_decimal(expected[name].get("cpus"))
        expected_nano_cpus = (
            int(expected_cpus * 1_000_000_000) if expected_cpus is not None else None
        )
        actual_memory = host_config.get("Memory") if isinstance(host_config, dict) else None
        actual_swap = host_config.get("MemorySwap") if isinstance(host_config, dict) else None
        actual_nano_cpus = host_config.get("NanoCpus") if isinstance(host_config, dict) else None
        if (
            name not in expected
            or name in seen
            or labels.get("com.docker.compose.project") != compose.get("name")
            or row.get("Id") != services[name].get("containerId")
            or row.get("Image") != services[name].get("imageId")
            or config.get("Image") != services[name].get("imageRef")
            or isinstance(actual_memory, bool)
            or not isinstance(actual_memory, int)
            or actual_memory != expected_memory
            or isinstance(actual_swap, bool)
            or not isinstance(actual_swap, int)
            or actual_swap != expected_memory
            or isinstance(actual_nano_cpus, bool)
            or not isinstance(actual_nano_cpus, int)
            or actual_nano_cpus != expected_nano_cpus
        ):
            raise StageTransitionError(
                f"actual memory limits differ from selected Compose for {name}"
            )
        seen.add(name)
        total_bytes += actual_memory
        total_nano_cpus += actual_nano_cpus
    maximum_bytes = max_memory_mib * preflight.MIB
    if total_bytes > maximum_bytes:
        raise StageTransitionError(
            "actual 14-service memory limits exceed the canonical host budget"
        )
    maximum_nano_cpus = int(Decimal(str(max_cpus)) * 1_000_000_000)
    if total_nano_cpus > maximum_nano_cpus:
        raise StageTransitionError("actual 14-service CPU limits exceed the canonical host budget")
    return {
        "serviceCount": len(seen),
        "totalMemoryBytes": total_bytes,
        "maxMemoryBytes": maximum_bytes,
        "totalNanoCpus": total_nano_cpus,
        "maxNanoCpus": maximum_nano_cpus,
    }


def _memory_update_plan_state(
    *,
    target: str,
    active_compose: dict[str, Any],
    candidate_compose: dict[str, Any],
    active_services: dict[str, dict[str, Any]],
    runtime_config_identity: dict[str, list[dict[str, Any]]],
    active_compose_files: list[Path],
    active_env_files: list[Path],
    candidate_compose_files: list[Path],
    candidate_env_files: list[Path],
    selection_record: dict[str, Any],
    journal_dir: Path,
    max_memory_mib: int,
    max_cpus: float,
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Freeze active and desired limits plus the selected running container identity."""

    active_service = _runtime_service_inventory(active_compose)[target]
    candidate_service = _runtime_service_inventory(candidate_compose)[target]
    active_memory = _memory_update_limit_bytes(active_service, f"active {target} service")
    candidate_memory = _memory_update_limit_bytes(candidate_service, f"candidate {target} service")
    if candidate_memory <= active_memory:
        raise StageTransitionError("memory update must increase a no-swap service limit")
    live_service = active_services.get(target)
    if not isinstance(live_service, dict):
        raise StageTransitionError("memory update target is missing from the active runtime")
    snapshot = _memory_update_container_snapshot(
        container_id=live_service["containerId"],
        project=active_compose["name"],
        service_name=target,
        expected_image={
            "imageRef": live_service["imageRef"],
            "imageId": live_service["imageId"],
        },
        expected_mounts=runtime_config_identity[target],
        expected_network_names=live_service["networkNames"],
        docker_prefix=docker_prefix,
    )
    if snapshot["memoryBytes"] != active_memory or snapshot["memorySwapBytes"] != active_memory:
        raise StageTransitionError(
            "active container memory and memory-swap differ from the selected Compose limits"
        )
    active_hash = _compose_service_config_hash(
        service_name=target,
        compose_files=active_compose_files,
        env_files=active_env_files,
        docker_prefix=docker_prefix,
    )
    candidate_hash = _compose_service_config_hash(
        service_name=target,
        compose_files=candidate_compose_files,
        env_files=candidate_env_files,
        docker_prefix=docker_prefix,
    )
    if active_hash == candidate_hash:
        raise StageTransitionError("Compose config hash did not capture the selected memory change")
    hash_proof: dict[str, str] | None = None
    if snapshot["composeConfigHash"] != active_hash:
        hash_proof = _prior_memory_update_hash_proof(
            selection_record=selection_record,
            selection_path=selection_record["path"],
            journal_dir=journal_dir,
            target=target,
            active_compose_hash=active_hash,
            container_compose_hash=snapshot["composeConfigHash"],
            active_service=live_service,
        )
        if hash_proof is None:
            raise StageTransitionError(
                "live Compose config hash is stale without selector-linked memory-update proof"
            )
    actual_capacity = _memory_update_actual_capacity(
        compose=active_compose,
        services=active_services,
        max_memory_mib=max_memory_mib,
        max_cpus=max_cpus,
        docker_prefix=docker_prefix,
    )
    return {
        "activeMemoryBytes": active_memory,
        "candidateMemoryBytes": candidate_memory,
        "activeComposeConfigHash": active_hash,
        "candidateComposeConfigHash": candidate_hash,
        "containerComposeConfigHash": snapshot["composeConfigHash"],
        "configHashRelation": (
            "MATCHES_ACTIVE" if hash_proof is None else "SELECTOR_JOURNAL_PROVEN_STALE"
        ),
        "configHashProofOperationId": (
            hash_proof["operationId"] if hash_proof is not None else None
        ),
        "configHashProofPlanSha256": (hash_proof["planSha256"] if hash_proof is not None else None),
        "containerConfigSha256": snapshot["containerConfigSha256"],
        "hostConfigSha256WithoutMemory": snapshot["hostConfigSha256WithoutMemory"],
        "processId": snapshot["processId"],
        "processStartedAt": snapshot["processStartedAt"],
        "restartCount": snapshot["restartCount"],
        "activeRuntimeMemoryBytes": actual_capacity["totalMemoryBytes"],
        "activeRuntimeNanoCpus": actual_capacity["totalNanoCpus"],
    }


def _verify_memory_update_snapshot(
    snapshot: dict[str, Any],
    memory_update: dict[str, Any],
    active_service: dict[str, Any],
    *,
    expected_memory_bytes: int,
) -> None:
    """Require a pinned container with only its intended memory values changed."""

    if (
        snapshot.get("composeConfigHash") != memory_update.get("containerComposeConfigHash")
        or snapshot.get("containerConfigSha256") != memory_update.get("containerConfigSha256")
        or snapshot.get("hostConfigSha256WithoutMemory")
        != memory_update.get("hostConfigSha256WithoutMemory")
    ):
        raise StageTransitionError("memory update changed immutable container configuration")
    if (
        snapshot.get("containerId") != active_service.get("containerId")
        or snapshot.get("imageRef") != active_service.get("imageRef")
        or snapshot.get("imageId") != active_service.get("imageId")
        or snapshot.get("processId") != memory_update.get("processId")
        or snapshot.get("processStartedAt") != memory_update.get("processStartedAt")
        or snapshot.get("restartCount") != memory_update.get("restartCount")
        or snapshot.get("memoryBytes") != expected_memory_bytes
        or snapshot.get("memorySwapBytes") != expected_memory_bytes
        or snapshot.get("health") != "healthy"
    ):
        raise StageTransitionError(
            "memory update read-back differs from the pinned container or exact desired limits"
        )


def _validate_image_update_topology(
    *,
    phase: str,
    target: str,
    active: dict[str, Any],
    candidate: dict[str, Any],
    active_lock: dict[str, Any],
    candidate_lock: dict[str, Any],
    old_services: dict[str, dict[str, Any]],
    new_services: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Allow one immutable service image change on an existing topology.

    中文：镜像更新沿用现有运行拓扑，只能替换一个已授权目标。
    """

    if not isinstance(target, str) or target not in IMAGE_UPDATE_TARGETS:
        raise StageTransitionError(
            "image update target must be Account Center, Cloud Management, Exchange, GitHub Issues, Hugging Face, Navigator, Persistence, or Workspace Authority"
        )
    if (
        target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
        and phase not in ACCOUNT_CENTER_IMAGE_UPDATE_PHASES
    ):
        raise StageTransitionError(
            "Account Center image updates require the selected Graph bootstrap or issued activation phase"
        )
    if target == GITHUB_ISSUES_IMAGE_UPDATE_TARGET and phase != "issued-connector-activation":
        raise StageTransitionError(
            "GitHub Issues image updates require issued-connector-activation and the existing connector runtime"
        )
    if target == HUGGINGFACE_IMAGE_UPDATE_TARGET and phase != "issued-connector-activation":
        raise StageTransitionError(
            "Hugging Face image updates require issued-connector-activation and the existing connector runtime"
        )
    if target == CLOUD_IMAGE_UPDATE_TARGET and phase not in PHASES:
        raise StageTransitionError(
            "Cloud Management image updates require provider-bootstrap or issued-connector-activation"
        )
    if target == PERSISTENCE_IMAGE_UPDATE_TARGET and phase != "issued-connector-activation":
        raise StageTransitionError(
            "Persistence image updates require issued-connector-activation and the existing connector runtime"
        )
    if (
        target == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET
        and phase not in WORKSPACE_AUTHORITY_IMAGE_UPDATE_PHASES
    ):
        raise StageTransitionError(
            "Workspace Authority image updates require the selected Graph bootstrap or issued activation phase"
        )
    if sum(name == "navigator" for name in new_services) != 1:
        raise StageTransitionError("image update must retain exactly one Navigator executor")
    if (
        target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
        and set(new_services) != ACCOUNT_CENTER_IMAGE_UPDATE_SERVICE_SET
    ):
        raise StageTransitionError(
            "Account Center image update must preserve the exact 15-service Graph runtime"
        )
    if (
        target == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET
        and set(new_services) != OFFICE_GRAPH_RUNTIME_SERVICE_SET
    ):
        raise StageTransitionError(
            "Workspace Authority image update must preserve the exact 15-service Graph runtime"
        )

    try:
        runtime_update._validate_lock_pair(active_lock, candidate_lock, [target])
        active_target = runtime_update._target_images(active, active_lock, [target])[target]
        candidate_target = runtime_update._target_images(candidate, candidate_lock, [target])[
            target
        ]
        active_ids = _lock_image_ids(active_lock)
        candidate_ids = _lock_image_ids(candidate_lock)
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "image update lock may change only the selected built service"
        ) from error
    if (
        active_target["image"] == candidate_target["image"]
        or active_target["imageId"] == candidate_target["imageId"]
    ):
        raise StageTransitionError("image update must use a distinct target reference and image ID")

    if set(old_services) != set(new_services):
        raise StageTransitionError("image update must keep the exact active service inventory")
    active_definition = copy.deepcopy(active)
    candidate_definition = copy.deepcopy(candidate)
    if target == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET:
        for source in (active, candidate):
            services = source.get("services")
            authority = (
                services.get(WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET)
                if isinstance(services, dict)
                else None
            )
            operator = services.get("directory-operator") if isinstance(services, dict) else None
            if (
                not isinstance(authority, dict)
                or not isinstance(operator, dict)
                or not isinstance(authority.get("image"), str)
                or not isinstance(operator.get("image"), str)
                or operator["image"] != authority["image"]
            ):
                raise StageTransitionError(
                    "directory-operator image must remain an exact alias of Workspace Authority"
                )
    for compose in (active_definition, candidate_definition):
        service = compose.get("services", {}).get(target)
        if not isinstance(service, dict) or not isinstance(service.get("image"), str):
            raise StageTransitionError(f"image update target {target} has no image reference")
        service.pop("image")
        if target == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET:
            services = compose.get("services", {})
            operator = services.get("directory-operator") if isinstance(services, dict) else None
            if not isinstance(operator, dict):
                raise StageTransitionError("Compose directory-operator service is unavailable")
            operator.pop("image", None)
    if active_definition != candidate_definition:
        raise StageTransitionError("image update may change only its selected service image")
    old_shape = _runtime_service_shape(old_services, "active image update")
    new_shape = _runtime_service_shape(new_services, "candidate image update")
    if old_shape != new_shape:
        raise StageTransitionError("image update must preserve its exact 14/15/16-service topology")

    target_images: dict[str, dict[str, Any]] = {}
    for service_name in new_services:
        active_image = _service_image(active, active_ids, service_name)
        candidate_image = _service_image(candidate, candidate_ids, service_name)
        if service_name == target:
            if active_image == candidate_image:
                raise StageTransitionError("selected image update target did not change")
        elif active_image != candidate_image:
            raise StageTransitionError(
                f"image update changes non-target service image {service_name}"
            )
        if _mounted_file_digests(active, service_name) != _mounted_file_digests(
            candidate, service_name
        ):
            raise StageTransitionError(
                f"image update changes mounted configuration for {service_name}"
            )
        target_images[service_name] = {
            "active": active_image,
            "candidate": candidate_image,
            "added": False,
            "configChanged": False,
        }
    return [target], target_images


def _validate_account_center_database_state(database: Any) -> None:
    """Require schema 9, clean SQLite checks, and the five fixed profile identities."""

    database_fields = {
        "databasePathSha256",
        "schemaVersion",
        "schemaSha256",
        "foreignKeyViolationCount",
        "quickCheck",
        "protectedProviderCount",
        "protectedProvidersSha256",
        "additionalMicrosoftProfileCount",
        "additionalMicrosoftProfileIdentitySha256",
    }
    optional_count_fields = {
        "recoveryPredicateCounts",
        "liveCredentialLeaseCounts",
    }
    if not isinstance(database, dict) or set(database) not in (
        database_fields,
        database_fields | optional_count_fields,
    ):
        raise StageTransitionError("Account Center database identity record is malformed")
    microsoft_count = database.get("additionalMicrosoftProfileCount")
    microsoft_digest = database.get("additionalMicrosoftProfileIdentitySha256")
    if (
        database.get("schemaVersion") != ACCOUNT_CENTER_DATABASE_USER_VERSION
        or isinstance(database.get("schemaVersion"), bool)
        or not isinstance(database.get("schemaVersion"), int)
        or database.get("foreignKeyViolationCount") != 0
        or isinstance(database.get("foreignKeyViolationCount"), bool)
        or not isinstance(database.get("foreignKeyViolationCount"), int)
        or database.get("quickCheck") != ["ok"]
        or database.get("protectedProviderCount") != len(ACCOUNT_CENTER_PROTECTED_PROFILE_PROVIDERS)
        or isinstance(database.get("protectedProviderCount"), bool)
        or not isinstance(database.get("protectedProviderCount"), int)
        or isinstance(microsoft_count, bool)
        or not isinstance(microsoft_count, int)
        or microsoft_count not in {0, 1}
        or (microsoft_count == 0 and microsoft_digest is not None)
        or (
            microsoft_count == 1
            and (
                not isinstance(microsoft_digest, str)
                or SHA256_RE.fullmatch(microsoft_digest) is None
            )
        )
        or any(
            not isinstance(database.get(field), str) or SHA256_RE.fullmatch(database[field]) is None
            for field in (
                "databasePathSha256",
                "schemaSha256",
                "protectedProvidersSha256",
            )
        )
    ):
        raise StageTransitionError(
            "Account Center image update requires schema 9 and the five fixed provider identities"
        )
    if optional_count_fields <= set(database):
        for field, expected_names in (
            ("recoveryPredicateCounts", ACCOUNT_CENTER_STARTUP_RECOVERY_PREDICATES),
            ("liveCredentialLeaseCounts", ACCOUNT_CENTER_LIVE_IDLE_COUNTS),
        ):
            counts = database.get(field)
            if (
                not isinstance(counts, dict)
                or set(counts) != expected_names
                or any(
                    isinstance(count, bool) or not isinstance(count, int) or count < 0
                    for count in counts.values()
                )
            ):
                raise StageTransitionError(
                    "Account Center startup-recovery/idle count record is malformed"
                )


def _account_center_database_state_matches_baseline(expected: Any, actual: Any) -> bool:
    """Compare a live database identity with the digest sealed in the private plan."""

    _validate_account_center_database_state(expected)
    _validate_account_center_database_state(actual)
    return actual == expected


def _parse_private_utc_timestamp(value: Any, label: str) -> datetime:
    """Parse a strict UTC timestamp used by the private startup-replay receipt."""

    if not isinstance(value, str) or not value:
        raise StageTransitionError(f"{label} timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise StageTransitionError(f"{label} timestamp is malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise StageTransitionError(f"{label} timestamp is not UTC")
    return parsed.astimezone(UTC)


def _account_center_preserved_unknown_summary(
    *,
    evidence_path: Path,
    evidence_record: dict[str, str],
    database_state: dict[str, Any],
    candidate_compose: dict[str, Any],
    candidate_image_id: str,
    candidate_image_lock_sha256: str,
) -> dict[str, Any]:
    """Seal one exact historical GitHub UNKNOWN row and its candidate startup replay proof."""

    _verify_operator_private_file_record(
        evidence_record, "Account Center unknown preservation evidence"
    )
    if evidence_record.get("path") != str(evidence_path.resolve(strict=True)):
        raise StageTransitionError("Account Center unknown preservation evidence path changed")
    evidence = _read_json(
        evidence_path,
        "Account Center unknown preservation evidence",
        maximum_bytes=ACCOUNT_CENTER_UNKNOWN_PRESERVATION_MAX_BYTES,
    )
    evidence_fields = {
        "schemaVersion",
        "state",
        "createdAtUtc",
        "currentDatabase",
        "historicalUnknown",
        "candidateStartupReplay",
    }
    current_database_fields = {
        "snapshotSha256",
        "schemaVersion",
        "schemaSha256",
        "foreignKeyViolationCount",
        "quickCheck",
        "protectedProviderCount",
        "protectedProvidersSha256",
        "additionalMicrosoftProfileCount",
        "additionalMicrosoftProfileIdentitySha256",
        "recoveryPredicateCounts",
        "liveCredentialLeaseCounts",
    }
    historical_fields = {
        "providerId",
        "connectionRef",
        "actorSha256",
        "rowSha256",
        "requestIdSha256",
        "operationIdSha256",
        "state",
        "rowCount",
        "columnCount",
    }
    replay_fields = {
        "state",
        "candidateImageId",
        "candidateImageLockSha256",
        "sourceDatabaseSnapshotSha256",
        "sourceSchemaVersion",
        "sourceSchemaSha256",
        "schemaBeforeSha256",
        "schemaAfterSha256",
        "allTableLogicalDigestBeforeSha256",
        "allTableLogicalDigestAfterSha256",
        "protectedFiveCredentialDigestBeforeSha256",
        "protectedFiveCredentialDigestAfterSha256",
        "networkMode",
        "liveDatabaseMounted",
        "secretMountsReadOnly",
        "credentialsExported",
        "activeContainerIdsUnchanged",
        "recoveryPredicateCounts",
        "replayScriptSha256",
    }
    if (
        set(evidence) != evidence_fields
        or evidence.get("schemaVersion") != ACCOUNT_CENTER_UNKNOWN_PRESERVATION_SCHEMA
        or evidence.get("state") != "OPERATOR_RECORDED_STARTUP_REPLAY_PASS"
    ):
        raise StageTransitionError(
            "Account Center unknown preservation evidence schema/state is invalid"
        )
    created_at = _parse_private_utc_timestamp(
        evidence.get("createdAtUtc"), "Account Center preservation evidence"
    )
    age_seconds = (datetime.now(UTC) - created_at).total_seconds()
    if age_seconds < -30 or age_seconds > ACCOUNT_CENTER_UNKNOWN_PRESERVATION_MAX_AGE_SECONDS:
        raise StageTransitionError("Account Center unknown preservation evidence is stale")

    current_database = evidence.get("currentDatabase")
    if not isinstance(current_database, dict) or set(current_database) != current_database_fields:
        raise StageTransitionError("Account Center current database evidence is malformed")
    database_baseline = {
        key: current_database[key]
        for key in current_database_fields
        if key not in {"snapshotSha256"}
    }
    database_baseline["databasePathSha256"] = database_state.get("databasePathSha256")
    _validate_account_center_database_state(database_baseline)
    _account_center_zero_recovery_counts(
        current_database.get("recoveryPredicateCounts"),
        current_database.get("liveCredentialLeaseCounts"),
        label="Account Center startup replay source snapshot",
    )
    if (
        any(
            current_database.get(field) != database_state.get(field)
            for field in (
                "schemaVersion",
                "schemaSha256",
                "foreignKeyViolationCount",
                "quickCheck",
                "protectedProviderCount",
                "protectedProvidersSha256",
                "additionalMicrosoftProfileCount",
                "additionalMicrosoftProfileIdentitySha256",
                "recoveryPredicateCounts",
                "liveCredentialLeaseCounts",
            )
        )
        or current_database.get("additionalMicrosoftProfileCount") != 0
        or not isinstance(current_database.get("snapshotSha256"), str)
        or SHA256_RE.fullmatch(current_database["snapshotSha256"]) is None
    ):
        raise StageTransitionError(
            "Account Center preservation snapshot differs from the live schema-9 profile baseline"
        )

    historical = evidence.get("historicalUnknown")
    if (
        not isinstance(historical, dict)
        or set(historical) != historical_fields
        or historical.get("providerId") != "github"
        or historical.get("state") != "UNKNOWN_RESULT"
        or historical.get("rowCount") != 1
        or isinstance(historical.get("rowCount"), bool)
        or historical.get("columnCount") != 20
        or isinstance(historical.get("columnCount"), bool)
        or not isinstance(historical.get("connectionRef"), str)
        or not isinstance(historical.get("actorSha256"), str)
        or SHA256_RE.fullmatch(str(historical.get("actorSha256"))) is None
        or not isinstance(historical.get("rowSha256"), str)
        or SHA256_RE.fullmatch(str(historical.get("rowSha256"))) is None
        or not isinstance(historical.get("requestIdSha256"), str)
        or SHA256_RE.fullmatch(str(historical.get("requestIdSha256"))) is None
        or not isinstance(historical.get("operationIdSha256"), str)
        or SHA256_RE.fullmatch(str(historical.get("operationIdSha256"))) is None
    ):
        raise StageTransitionError("sealed historical GitHub UNKNOWN row is malformed")
    try:
        canonical_connection_ref = str(uuid.UUID(historical["connectionRef"]))
    except (ValueError, TypeError, AttributeError) as error:
        raise StageTransitionError(
            "sealed historical GitHub connection ref is malformed"
        ) from error
    if canonical_connection_ref != historical["connectionRef"]:
        raise StageTransitionError("sealed historical GitHub connection ref is not canonical")
    actor = preflight._connector_bootstrap_actor(candidate_compose)
    actor_sha256 = _sha256_bytes(_canonical_json(actor))
    if historical["actorSha256"] != actor_sha256:
        raise StageTransitionError(
            "historical GitHub UNKNOWN row actor differs from the selected owner"
        )
    bindings = _candidate_config_json(candidate_compose, "connector-bindings").get("bindings")
    github_refs = (
        {
            row.get("connectionRef")
            for row in bindings
            if isinstance(row, dict) and row.get("providerId") == "github"
        }
        if isinstance(bindings, list)
        else set()
    )
    if historical["connectionRef"] not in github_refs:
        raise StageTransitionError(
            "historical GitHub UNKNOWN row does not join a frozen GitHub binding"
        )

    replay = evidence.get("candidateStartupReplay")
    if not isinstance(replay, dict) or set(replay) != replay_fields:
        raise StageTransitionError("candidate startup replay receipt is malformed")
    recovery_predicates = replay.get("recoveryPredicateCounts")
    if (
        replay.get("state") != "PASS"
        or replay.get("candidateImageId") != candidate_image_id
        or replay.get("candidateImageLockSha256") != candidate_image_lock_sha256
        or replay.get("sourceDatabaseSnapshotSha256") != current_database["snapshotSha256"]
        or replay.get("sourceSchemaVersion") != ACCOUNT_CENTER_DATABASE_USER_VERSION
        or replay.get("sourceSchemaSha256") != current_database["schemaSha256"]
        or replay.get("schemaBeforeSha256") != replay.get("schemaAfterSha256")
        or replay.get("schemaBeforeSha256") != current_database["schemaSha256"]
        or replay.get("allTableLogicalDigestBeforeSha256")
        != replay.get("allTableLogicalDigestAfterSha256")
        or replay.get("protectedFiveCredentialDigestBeforeSha256")
        != replay.get("protectedFiveCredentialDigestAfterSha256")
        or replay.get("protectedFiveCredentialDigestBeforeSha256")
        != current_database["protectedProvidersSha256"]
        or replay.get("networkMode") != "NONE"
        or replay.get("liveDatabaseMounted") is not False
        or replay.get("secretMountsReadOnly") is not True
        or replay.get("credentialsExported") is not False
        or replay.get("activeContainerIdsUnchanged") is not True
        or not isinstance(replay.get("replayScriptSha256"), str)
        or SHA256_RE.fullmatch(replay["replayScriptSha256"]) is None
        or not isinstance(recovery_predicates, dict)
        or set(recovery_predicates) != ACCOUNT_CENTER_STARTUP_RECOVERY_PREDICATES
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value != 0
            for value in recovery_predicates.values()
        )
        or not isinstance(replay.get("schemaBeforeSha256"), str)
        or SHA256_RE.fullmatch(replay["schemaBeforeSha256"]) is None
        or not isinstance(replay.get("allTableLogicalDigestBeforeSha256"), str)
        or SHA256_RE.fullmatch(replay["allTableLogicalDigestBeforeSha256"]) is None
        or not isinstance(replay.get("protectedFiveCredentialDigestBeforeSha256"), str)
        or SHA256_RE.fullmatch(replay["protectedFiveCredentialDigestBeforeSha256"]) is None
    ):
        raise StageTransitionError(
            "candidate startup replay does not prove unchanged schema/data and zero recovery actions"
        )
    return {
        "schemaVersion": 1,
        "operatorEvidenceSha256": evidence_record["sha256"],
        "providerId": "github",
        "connectionRef": canonical_connection_ref,
        "connectionRefSha256": _sha256_bytes(canonical_connection_ref.encode("utf-8")),
        "actorSha256": actor_sha256,
        "invocationRowSha256": historical["rowSha256"],
        "requestIdSha256": historical["requestIdSha256"],
        "operationIdSha256": historical["operationIdSha256"],
        "startupReplaySha256": _sha256_bytes(_canonical_json(replay)),
        "startupReplayScriptSha256": replay["replayScriptSha256"],
        "candidateImageId": candidate_image_id,
        "candidateImageLockSha256": candidate_image_lock_sha256,
        "currentDatabaseSnapshotSha256": current_database["snapshotSha256"],
    }


def _validate_account_center_image_update_state(value: Any) -> None:
    """Require the sealed Account Center database state and exact 15-service budgets."""

    allowed_fields = {"database", "runtimeCapacity", "preservedUnknown"}
    if not isinstance(value, dict) or set(value) not in (
        allowed_fields - {"preservedUnknown"},
        allowed_fields,
    ):
        raise StageTransitionError("Account Center image update state is malformed")
    database_state = value.get("database")
    _validate_account_center_database_state(database_state)
    if not isinstance(database_state, dict) or not {
        "recoveryPredicateCounts",
        "liveCredentialLeaseCounts",
    } <= set(database_state):
        raise StageTransitionError(
            "Account Center image update requires a complete startup recovery baseline"
        )
    _account_center_zero_recovery_counts(
        database_state["recoveryPredicateCounts"],
        database_state["liveCredentialLeaseCounts"],
        label="Account Center image update baseline",
    )
    if "preservedUnknown" in value:
        _validate_account_center_preserved_unknown_state(value["preservedUnknown"])
    capacity = value.get("runtimeCapacity")
    capacity_fields = {
        "serviceCount",
        "totalMemoryBytes",
        "maxMemoryBytes",
        "totalNanoCpus",
        "maxNanoCpus",
    }
    maximum_memory = 3584 * preflight.MIB
    maximum_nano_cpus = 1_950_000_000
    if (
        not isinstance(capacity, dict)
        or set(capacity) != capacity_fields
        or capacity.get("serviceCount") != 15
        or isinstance(capacity.get("serviceCount"), bool)
        or not isinstance(capacity.get("serviceCount"), int)
        or capacity.get("maxMemoryBytes") != maximum_memory
        or isinstance(capacity.get("maxMemoryBytes"), bool)
        or not isinstance(capacity.get("maxMemoryBytes"), int)
        or capacity.get("maxNanoCpus") != maximum_nano_cpus
        or isinstance(capacity.get("maxNanoCpus"), bool)
        or not isinstance(capacity.get("maxNanoCpus"), int)
        or any(
            isinstance(capacity.get(field), bool)
            or not isinstance(capacity.get(field), int)
            or capacity[field] < 0
            for field in ("totalMemoryBytes", "totalNanoCpus")
        )
        or capacity.get("totalMemoryBytes", maximum_memory + 1) > maximum_memory
        or capacity.get("totalNanoCpus", maximum_nano_cpus + 1) > maximum_nano_cpus
    ):
        raise StageTransitionError(
            "Account Center image update must retain the 15-service 3584 MiB/1.95 CPU budget"
        )


def _validate_account_center_preserved_unknown_state(value: Any) -> None:
    """Validate one private, digest-bound GitHub UNKNOWN preservation plan record."""

    fields = {
        "schemaVersion",
        "operatorEvidenceSha256",
        "providerId",
        "connectionRef",
        "connectionRefSha256",
        "actorSha256",
        "invocationRowSha256",
        "requestIdSha256",
        "operationIdSha256",
        "startupReplaySha256",
        "startupReplayScriptSha256",
        "candidateImageId",
        "candidateImageLockSha256",
        "currentDatabaseSnapshotSha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schemaVersion") != 1
        or isinstance(value.get("schemaVersion"), bool)
        or value.get("providerId") != "github"
        or not isinstance(value.get("connectionRef"), str)
        or any(
            not isinstance(value.get(field), str) or SHA256_RE.fullmatch(value[field]) is None
            for field in (
                "operatorEvidenceSha256",
                "connectionRefSha256",
                "actorSha256",
                "invocationRowSha256",
                "requestIdSha256",
                "operationIdSha256",
                "startupReplaySha256",
                "startupReplayScriptSha256",
                "candidateImageLockSha256",
                "currentDatabaseSnapshotSha256",
            )
        )
        or not isinstance(value.get("candidateImageId"), str)
        or IMAGE_ID_RE.fullmatch(value["candidateImageId"]) is None
    ):
        raise StageTransitionError("Account Center preserved UNKNOWN plan record is malformed")
    try:
        canonical_connection_ref = str(uuid.UUID(value["connectionRef"]))
    except (ValueError, TypeError, AttributeError) as error:
        raise StageTransitionError(
            "Account Center preserved UNKNOWN connection reference is malformed"
        ) from error
    if canonical_connection_ref != value["connectionRef"]:
        raise StageTransitionError(
            "Account Center preserved UNKNOWN connection reference is not canonical"
        )
    if value["connectionRefSha256"] != _sha256_bytes(value["connectionRef"].encode("utf-8")):
        raise StageTransitionError(
            "Account Center preserved UNKNOWN connection reference digest changed"
        )


def _account_center_profile_logical_rows(rows: list[tuple[Any, ...]]) -> list[list[Any]]:
    """Normalize all profile metadata while replacing encrypted blobs with local hashes."""

    result: list[list[Any]] = []
    for row in rows:
        if len(row) != len(ACCOUNT_CENTER_PROFILE_COLUMN_NAMES):
            raise StageTransitionError("Account Center provider profile schema changed")
        normalized: list[dict[str, Any]] = []
        for index, value in enumerate(row):
            if index in ACCOUNT_CENTER_PROFILE_BLOB_COLUMN_INDICES:
                if value is None:
                    normalized.append({"sqliteType": "NoneType", "value": None})
                elif isinstance(value, (bytes, bytearray, memoryview)):
                    blob_bytes = bytes(value)
                    normalized.append(
                        {
                            "sqliteType": "blob",
                            "sha256": _sha256_bytes(blob_bytes),
                            "bytes": len(blob_bytes),
                        }
                    )
                else:
                    raise StageTransitionError(
                        "Account Center profile credential blob has an unsupported SQLite type"
                    )
            else:
                normalized.append({"sqliteType": type(value).__name__, "value": value})
        result.append(normalized)
    return result


def _account_center_startup_recovery_counts(
    connection: sqlite3.Connection, *, now: int
) -> tuple[dict[str, int], dict[str, int]]:
    """Count the exact 439 startup mutations and schema-valid live credential leases."""

    def count(query: str, parameters: tuple[Any, ...] = ()) -> int:
        row = connection.execute(query, parameters).fetchone()
        if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 0:
            raise StageTransitionError("Account Center startup recovery count is malformed")
        return row[0]

    recovery = {
        "provider_profiles_active_refresh_claims": count(
            "SELECT COUNT(*) FROM provider_profiles WHERE status='active' "
            "AND refresh_claim_id IS NOT NULL"
        ),
        "provider_profiles_active_stale_refresh_claims": count(
            "SELECT COUNT(*) FROM provider_profiles WHERE status='active' "
            "AND refresh_claim_id IS NOT NULL AND refresh_claimed_at<=?",
            (now - 45,),
        ),
        "accounts_refresh_pending": count(
            "SELECT COUNT(*) FROM accounts WHERE credential_status='refresh_pending'"
        ),
        "accounts_active_refresh_claims": count(
            "SELECT COUNT(*) FROM accounts WHERE refresh_claim_id IS NOT NULL"
        ),
        "provider_profiles_hf_reauth_lease_deletions": count(
            "SELECT COUNT(*) FROM provider_credential_leases WHERE profile_ref IN "
            "(SELECT profile_ref FROM provider_profiles WHERE provider='huggingface' "
            "AND auth_status='reauthorization_required')"
        ),
        "accounts_reauth_lease_deletions": count(
            "SELECT COUNT(*) FROM credential_leases WHERE account_ref IN "
            "(SELECT account_ref FROM accounts WHERE credential_status='reauthorization_required')"
        ),
        "accounts_pending_remote_revocation": count(
            "SELECT COUNT(*) FROM accounts WHERE status='unlinked' "
            "AND remote_revocation_status='pending'"
        ),
        "microsoft_entra_processing_sessions": count(
            "SELECT COUNT(*) FROM microsoft_entra_oauth_sessions WHERE status='processing'"
        ),
        "github_processing_link_sessions_without_blob": count(
            "SELECT COUNT(*) FROM link_sessions WHERE provider='github' AND status='processing' "
            "AND pending_credential_blob IS NULL"
        ),
        "github_interrupted_callback_revocations": count(
            "SELECT COUNT(*) FROM link_sessions WHERE provider='github' AND status='processing' "
            "AND pending_credential_blob IS NOT NULL"
        ),
        "hf_device_starting_stale": count(
            "SELECT COUNT(*) FROM huggingface_device_renewal_sessions WHERE status='starting' "
            "AND created_at<=?",
            (now - 60,),
        ),
        "hf_device_active_poll_claims": count(
            "SELECT COUNT(*) FROM huggingface_device_renewal_sessions "
            "WHERE poll_claim_id IS NOT NULL"
        ),
        "hf_device_poll_stale_foreign_claim": count(
            "SELECT COUNT(*) FROM huggingface_device_renewal_sessions "
            "WHERE status='waiting_user' AND poll_claim_id IS NOT NULL "
            "AND poll_claimed_at<=?",
            (now - 45,),
        ),
        "accounts_interrupted_unlink_commands": count(
            "SELECT COUNT(*) FROM accounts WHERE status='unlinked' "
            "AND remote_revocation_status='unknown' AND remote_revocation_command_hash IS NOT NULL"
        ),
        "pending_idempotency": count(
            "SELECT COUNT(*) FROM command_idempotency WHERE status='pending'"
        ),
    }
    live_leases = {
        "activeProviderCredentialLeases": count(
            "SELECT COUNT(*) FROM provider_credential_leases AS lease "
            "JOIN provider_profiles AS profile ON profile.profile_ref=lease.profile_ref "
            "WHERE lease.redeemed_at IS NULL AND lease.expires_at>? "
            "AND profile.status='active' AND profile.auth_status='healthy' "
            "AND lease.anchor_issuer=profile.anchor_issuer "
            "AND lease.anchor_subject=profile.anchor_subject "
            "AND lease.organization_id=profile.organization_id "
            "AND lease.workspace_id IS profile.workspace_id",
            (now,),
        ),
        "activeAccountCredentialLeases": count(
            "SELECT COUNT(*) FROM credential_leases AS lease "
            "JOIN accounts AS account ON account.account_ref=lease.account_ref "
            "WHERE lease.redeemed_at IS NULL AND lease.expires_at>? "
            "AND account.status='active' AND account.credential_status IN ('ready','refresh_pending') "
            "AND account.credential_blob IS NOT NULL "
            "AND lease.anchor_issuer=account.anchor_issuer "
            "AND lease.anchor_subject=account.anchor_subject "
            "AND lease.organization_id=account.organization_id "
            "AND lease.workspace_id IS account.workspace_id",
            (now,),
        ),
    }
    return recovery, live_leases


def _account_center_zero_recovery_counts(recovery: Any, live_leases: Any, *, label: str) -> None:
    """Require every pinned 439 startup mutation and live credential lease count to be empty."""

    if (
        not isinstance(recovery, dict)
        or set(recovery) != ACCOUNT_CENTER_STARTUP_RECOVERY_PREDICATES
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value != 0
            for value in recovery.values()
        )
        or not isinstance(live_leases, dict)
        or set(live_leases) != ACCOUNT_CENTER_LIVE_IDLE_COUNTS
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value != 0
            for value in live_leases.values()
        )
    ):
        raise StageTransitionError(
            f"{label} has Account Center startup recovery or credential claims"
        )


def _account_center_database_state(
    *,
    compose: dict[str, Any],
    project: str,
    container_id: str,
    image_ref: str,
    image_id: str,
    docker_prefix: list[str],
    include_startup_recovery_counts: bool = False,
) -> dict[str, Any]:
    """Read the mounted Account Center database without exposing profile values."""

    if (
        FULL_CONTAINER_ID_RE.fullmatch(container_id) is None
        or IMAGE_ID_RE.fullmatch(image_id) is None
    ):
        raise StageTransitionError("Account Center database container identity is malformed")
    raw = runtime_update._decode_json_output(
        runtime_update._checked_stdout(
            [*docker_prefix, "inspect", container_id],
            label="Account Center database container inspection",
        ),
        "Account Center database container inspection",
    )
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise StageTransitionError("Account Center database container inspection is ambiguous")
    container = raw[0]
    config = container.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    state = container.get("State")
    if (
        container.get("Id") != container_id
        or container.get("Image") != image_id
        or not isinstance(config, dict)
        or config.get("Image") != image_ref
        or not isinstance(labels, dict)
        or labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
        or not isinstance(state, dict)
        or state.get("Running") is not True
        or state.get("Paused") is True
    ):
        raise StageTransitionError("Account Center database container is not the exact live target")

    mounts = container.get("Mounts")
    data_mounts = (
        [
            row
            for row in mounts
            if isinstance(row, dict)
            and row.get("Type") == "volume"
            and row.get("Destination") == "/data"
        ]
        if isinstance(mounts, list)
        else []
    )
    environment = config.get("Env")
    if (
        len(data_mounts) != 1
        or not isinstance(data_mounts[0].get("Source"), str)
        or not isinstance(environment, list)
    ):
        raise StageTransitionError("Account Center SQLite volume/configuration is ambiguous")
    environment_values: dict[str, str] = {}
    for entry in environment:
        if not isinstance(entry, str) or "=" not in entry:
            raise StageTransitionError("Account Center environment is malformed")
        name, value = entry.split("=", 1)
        if name in environment_values:
            raise StageTransitionError("Account Center environment contains duplicate keys")
        environment_values[name] = value
    compose_service = compose.get("services", {}).get(ACCOUNT_CENTER_IMAGE_UPDATE_TARGET)
    compose_environment = (
        compose_service.get("environment") if isinstance(compose_service, dict) else None
    )
    if isinstance(compose_environment, dict):
        expected_environment = compose_environment
    elif isinstance(compose_environment, list):
        expected_environment = {}
        for entry in compose_environment:
            if not isinstance(entry, str) or "=" not in entry:
                raise StageTransitionError("rendered Account Center environment is malformed")
            name, value = entry.split("=", 1)
            if name in expected_environment:
                raise StageTransitionError("rendered Account Center environment is ambiguous")
            expected_environment[name] = value
    else:
        raise StageTransitionError("rendered Account Center environment is unavailable")
    if any(
        not isinstance(name, str)
        or not isinstance(value, str)
        or environment_values.get(name) != value
        for name, value in expected_environment.items()
    ):
        raise StageTransitionError(
            "live Account Center environment differs from the sealed Compose configuration"
        )
    connection: sqlite3.Connection | None = None
    startup_recovery_counts: dict[str, int] | None = None
    live_idle_counts: dict[str, int] | None = None
    try:
        activation = json.loads(
            environment_values["CYRENE_CAPABILITY_CONFIGURATION_JSON"],
            object_pairs_hook=preflight._unique_json_object,
        )
        database_path = Path(data_mounts[0]["Source"]) / "accounts.sqlite3"
        resolved_database_path = database_path.resolve(strict=True)
    except (
        KeyError,
        OSError,
        RuntimeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        raise StageTransitionError(
            "Account Center SQLite source/configuration is unavailable"
        ) from error
    if (
        environment_values.get("CYRENE_PLUGIN_STORAGE_PATH") != "/data"
        or not isinstance(activation, dict)
        or activation.get("storage_path", "/data/accounts.sqlite3") != "/data/accounts.sqlite3"
        or database_path.is_symlink()
        or not resolved_database_path.is_file()
    ):
        raise StageTransitionError("Account Center SQLite path differs from its selected volume")

    try:
        connection = sqlite3.connect(
            f"{resolved_database_path.as_uri()}?mode=ro", uri=True, timeout=15
        )
        connection.execute("BEGIN")
        schema = list(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        )
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        foreign_key_count = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        quick_check = [row[0] for row in connection.execute("PRAGMA quick_check")]
        profile_column_names = tuple(
            row[1] for row in connection.execute("PRAGMA table_info(provider_profiles)")
        )
        if profile_column_names != ACCOUNT_CENTER_PROFILE_COLUMN_NAMES:
            raise StageTransitionError(
                "Account Center provider profile columns differ from the pinned schema-9 ABI"
            )
        profiles = list(
            connection.execute(
                f"SELECT {ACCOUNT_CENTER_PROFILE_COLUMNS} "
                "FROM provider_profiles ORDER BY profile_ref"
            )
        )
        if include_startup_recovery_counts:
            startup_recovery_counts, live_idle_counts = _account_center_startup_recovery_counts(
                connection, now=int(time.time())
            )
    except sqlite3.Error as error:
        raise StageTransitionError(
            "Account Center SQLite read-only identity check failed"
        ) from error
    finally:
        if connection is not None:
            connection.close()

    logical_profiles = _account_center_profile_logical_rows(profiles)
    row_digests_by_provider: dict[str, str] = {}
    for row in logical_profiles:
        provider_cell = row[5]
        if not isinstance(provider_cell, dict) or provider_cell.get("sqliteType") != "str":
            raise StageTransitionError("Account Center provider profile identity is malformed")
        provider = provider_cell.get("value")
        if not isinstance(provider, str) or provider in row_digests_by_provider:
            raise StageTransitionError(
                "Account Center provider profile identity is duplicate or malformed"
            )
        row_digests_by_provider[provider] = _sha256_bytes(_canonical_json(row))
    protected_provider_digests = {
        provider: row_digests_by_provider[provider]
        for provider in ACCOUNT_CENTER_PROTECTED_PROFILE_PROVIDERS
        if provider in row_digests_by_provider
    }
    extras = {
        provider: digest
        for provider, digest in row_digests_by_provider.items()
        if provider not in ACCOUNT_CENTER_PROTECTED_PROFILE_PROVIDERS
    }
    if (
        len(protected_provider_digests) != len(ACCOUNT_CENTER_PROTECTED_PROFILE_PROVIDERS)
        or len(extras) > 1
    ):
        raise StageTransitionError(
            "Account Center does not contain the five fixed provider profile identities"
        )
    additional_microsoft_digest = None
    if extras:
        extra_provider = next(iter(extras))
        extra = next(row for row in logical_profiles if row[5].get("value") == extra_provider)
        try:
            actor = preflight._connector_bootstrap_actor(compose)
        except preflight.PreflightError as error:
            raise StageTransitionError(
                "Account Center Microsoft profile owner/workspace binding is unavailable"
            ) from error
        if (
            not isinstance(actor, dict)
            or any(
                not isinstance(actor.get(field), str) or not actor[field]
                for field in ("issuer", "subject", "workspaceId")
            )
            or extra_provider != ACCOUNT_CENTER_MICROSOFT_PROFILE_PROVIDER
            or extra[1].get("value") != actor.get("issuer")
            or extra[2].get("value") != actor.get("subject")
            or extra[4].get("value") != actor.get("workspaceId")
        ):
            raise StageTransitionError(
                "additional Microsoft profile is not bound to the selected Office owner/workspace"
            )
        additional_microsoft_digest = _sha256_bytes(
            _canonical_json(
                {
                    "columns": list(ACCOUNT_CENTER_PROFILE_COLUMN_NAMES),
                    "providers": {extra_provider: extras[extra_provider]},
                }
            )
        )
    protected_digest = _sha256_bytes(
        _canonical_json(
            {
                "columns": list(ACCOUNT_CENTER_PROFILE_COLUMN_NAMES),
                "providers": protected_provider_digests,
            }
        )
    )
    result = {
        "databasePathSha256": _sha256_bytes(str(resolved_database_path).encode("utf-8")),
        "schemaVersion": user_version,
        "schemaSha256": _sha256_bytes(_canonical_json(schema)),
        "foreignKeyViolationCount": foreign_key_count,
        "quickCheck": quick_check,
        "protectedProviderCount": len(protected_provider_digests),
        "protectedProvidersSha256": protected_digest,
        "additionalMicrosoftProfileCount": len(extras),
        "additionalMicrosoftProfileIdentitySha256": additional_microsoft_digest,
    }
    if include_startup_recovery_counts:
        if startup_recovery_counts is None or live_idle_counts is None:
            raise StageTransitionError("Account Center recovery counts were not captured")
        _account_center_zero_recovery_counts(
            startup_recovery_counts, live_idle_counts, label="Account Center database"
        )
        result["recoveryPredicateCounts"] = startup_recovery_counts
        result["liveCredentialLeaseCounts"] = live_idle_counts
    _validate_account_center_database_state(result)
    return result


def _validate_listener_repair_topology(
    *,
    phase: str,
    active: dict[str, Any],
    candidate: dict[str, Any],
    active_lock: dict[str, Any],
    candidate_lock: dict[str, Any],
    old_services: dict[str, dict[str, Any]],
    new_services: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Allow only the closed HF/Cloud listener argument replacement on 14 services.

    中文：Bootstrap listener repair 只能改两项 listen 地址，镜像与其它配置必须原样保留。
    """

    if phase != "provider-bootstrap":
        raise StageTransitionError("provider listener repair is limited to provider-bootstrap")
    old_shape = _runtime_service_shape(old_services, "active listener repair")
    new_shape = _runtime_service_shape(new_services, "candidate listener repair")
    if old_shape != new_shape:
        raise StageTransitionError(
            "provider listener repair must preserve the runtime service inventory"
        )
    if not set(PROVIDER_LISTENER_REPAIR_TARGETS) <= set(old_services):
        raise StageTransitionError("provider listener repair requires both HF and Cloud services")
    active_definition = copy.deepcopy(active)
    candidate_definition = copy.deepcopy(candidate)
    for service_name in PROVIDER_LISTENER_REPAIR_TARGETS:
        active_service = active_definition.get("services", {}).get(service_name)
        candidate_service = candidate_definition.get("services", {}).get(service_name)
        if not isinstance(active_service, dict) or not isinstance(candidate_service, dict):
            raise StageTransitionError(
                f"provider listener repair service {service_name} is missing"
            )
        active_command = active_service.get("command")
        if active_command is not None and active_command != list(
            PROVIDER_DEFAULT_COMMANDS[service_name]
        ):
            raise StageTransitionError(
                f"active {service_name} command is not the exact hostname-bound image default"
            )
        if candidate_service.get("command") != list(PROVIDER_WILDCARD_COMMANDS[service_name]):
            raise StageTransitionError(
                f"candidate {service_name} command must change only its listener to 0.0.0.0:7443"
            )
        active_service.pop("command", None)
        candidate_service.pop("command", None)
    if active_definition != candidate_definition:
        raise StageTransitionError(
            "provider listener repair may change only the HF and Cloud listener commands"
        )
    if active_lock != candidate_lock:
        raise StageTransitionError("provider listener repair must preserve the exact image lock")
    active_ids = _lock_image_ids(active_lock)
    candidate_ids = _lock_image_ids(candidate_lock)
    target_images: dict[str, dict[str, Any]] = {}
    for service_name in _runtime_service_order_for(new_services):
        active_image = _service_image(active, active_ids, service_name)
        candidate_image = _service_image(candidate, candidate_ids, service_name)
        if active_image != candidate_image:
            raise StageTransitionError(f"provider listener repair changes image for {service_name}")
        if _mounted_file_digests(active, service_name) != _mounted_file_digests(
            candidate, service_name
        ):
            raise StageTransitionError(
                f"provider listener repair changes mounted configuration for {service_name}"
            )
        target_images[service_name] = {
            "active": active_image,
            "candidate": candidate_image,
            "added": False,
            "configChanged": False,
        }
    return list(PROVIDER_LISTENER_REPAIR_TARGETS), target_images


def _expected_mount_identity(compose: dict[str, Any], service_name: str) -> list[dict[str, Any]]:
    """Resolve file-backed and named-volume mounts for one rendered Compose service."""

    services = compose.get("services", {})
    service = services.get(service_name) if isinstance(services, dict) else None
    if not isinstance(service, dict):
        raise StageTransitionError(
            f"Compose service {service_name} is unavailable for mount identity"
        )
    result: list[dict[str, Any]] = []
    for row in _mount_rows(service, "volumes"):
        mount_type = row.get("type")
        source = row.get("source")
        target = row.get("target")
        if mount_type == "bind":
            if not isinstance(source, str) or not isinstance(target, str):
                raise StageTransitionError(f"{service_name} bind mount is malformed")
            try:
                resolved_source = str(Path(source).resolve(strict=True))
            except OSError as error:
                raise StageTransitionError(
                    f"{service_name} bind mount source is unavailable"
                ) from error
            read_only = row.get("read_only") is True
            resolved_path = Path(resolved_source)
            if target == AUTOMATION_HEALTH_TARGET:
                if not read_only:
                    raise StageTransitionError("automation health mount must be read-only")
                source_metadata = _validate_automation_health_source(Path(source), resolved_source)
            else:
                source_metadata = resolved_path.stat()
            if not (stat.S_ISDIR(source_metadata.st_mode) or stat.S_ISREG(source_metadata.st_mode)):
                raise StageTransitionError(f"{service_name} bind source is not a file or directory")
            identity = {
                "type": "bind",
                "source": resolved_source,
                "target": target,
                "readOnly": read_only,
                "sourceKind": "directory" if stat.S_ISDIR(source_metadata.st_mode) else "file",
                "sourceUid": source_metadata.st_uid,
                "sourceMode": stat.S_IMODE(source_metadata.st_mode),
            }
            if target == AUTOMATION_HEALTH_TARGET:
                identity["sourceGid"] = source_metadata.st_gid
            if read_only and resolved_path.is_file():
                identity["contentSha256"] = _sha256_file(resolved_path)
            elif (
                read_only
                and stat.S_ISDIR(source_metadata.st_mode)
                and target != AUTOMATION_HEALTH_TARGET
            ):
                directory_contents = _directory_manifest(
                    resolved_path, "Teams authorization config"
                )
                identity["contentSha256"] = _sha256_bytes(_canonical_json(directory_contents))
            result.append(identity)
        elif mount_type == "volume":
            if not isinstance(source, str) or not isinstance(target, str):
                raise StageTransitionError(f"{service_name} named-volume mount is malformed")
            if target == AUTOMATION_HEALTH_TARGET:
                raise StageTransitionError(
                    "automation health must use its fixed read-only directory bind"
                )
            volume_row = compose.get("volumes", {}).get(source, {})
            volume_name = volume_row.get("name") if isinstance(volume_row, dict) else None
            actual_name = (
                volume_name
                if isinstance(volume_name, str) and volume_name
                else f"{compose['name']}_{source}"
            )
            result.append(
                {
                    "type": "volume",
                    "source": actual_name,
                    "target": target,
                    "readOnly": row.get("read_only") is True,
                }
            )
        elif mount_type is not None:
            raise StageTransitionError(f"{service_name} has an unsupported mount type")
    for field in ("configs", "secrets"):
        definitions = compose.get(field, {})
        if not isinstance(definitions, dict):
            raise StageTransitionError(f"Compose {field} inventory is malformed")
        for row in _mount_rows(service, field):
            name = row.get("source")
            target = row.get("target")
            if (
                field == "secrets"
                and isinstance(target, str)
                and not target.startswith("/")
                and (
                    not target
                    or target in {".", ".."}
                    or "/" in target
                    or "\\" in target
                    or "\x00" in target
                )
            ):
                raise StageTransitionError(f"{service_name} Compose secret target is malformed")
            if field == "secrets" and isinstance(target, str) and not target.startswith("/"):
                target = f"/run/secrets/{target}"
            definition = definitions.get(name) if isinstance(name, str) else None
            source = definition.get("file") if isinstance(definition, dict) else None
            if not isinstance(source, str) or not isinstance(target, str):
                raise StageTransitionError(f"{service_name} {field} mount is unresolved")
            if target == AUTOMATION_HEALTH_TARGET:
                raise StageTransitionError(
                    "automation health must use its fixed read-only directory bind"
                )
            try:
                resolved_source = str(Path(source).resolve(strict=True))
            except OSError as error:
                raise StageTransitionError(f"{service_name} {field} file is unavailable") from error
            source_metadata = Path(resolved_source).stat()
            result.append(
                {
                    "type": "bind",
                    "source": resolved_source,
                    "target": target,
                    "readOnly": True,
                    "sourceKind": "directory" if stat.S_ISDIR(source_metadata.st_mode) else "file",
                    "sourceUid": source_metadata.st_uid,
                    "sourceMode": stat.S_IMODE(source_metadata.st_mode),
                    "contentSha256": _sha256_file(Path(resolved_source)),
                }
            )
    return sorted(result, key=lambda row: (row["target"], row["type"], row["source"]))


def _runtime_config_identity_for_compose(
    compose: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Fingerprint static inputs and pin stateful mounts for each selected runtime service."""

    return {
        name: _expected_mount_identity(compose, name)
        for name in sorted(_runtime_service_inventory(compose))
    }


def _candidate_runtime_config_identity(
    *,
    active_identity: dict[str, list[dict[str, Any]]],
    candidate_compose: dict[str, Any],
    target_services: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Use candidate mounts only for recreated services and retain live mounts elsewhere."""

    candidate_services = set(_runtime_service_inventory(candidate_compose))
    if not target_services <= candidate_services:
        raise StageTransitionError("target mount identity names a missing candidate service")
    if not candidate_services - target_services <= set(active_identity):
        raise StageTransitionError("untargeted candidate service has no active mount identity")
    identity = {
        name: (
            _expected_mount_identity(candidate_compose, name)
            if name in target_services
            else active_identity[name]
        )
        for name in sorted(candidate_services)
    }
    return _validate_runtime_config_identity(
        identity,
        expected_services=candidate_services,
        label="candidate runtime configuration identity",
    )


def _validate_image_update_selection_contract(
    *,
    target: str | None,
    memory_update_target: str | None = None,
    phase: str,
    project: str,
    selection_record: Any,
    selection_path: str,
    active_config_dir: str,
    candidate_config_dir: str,
    active_secrets_dir: str,
    active_inputs: dict[str, Any],
    candidate_inputs: dict[str, Any],
    runtime_config_identity: Any,
) -> None:
    """Bind a one-service update to the existing selector and frozen runtime identity."""

    if target is None and memory_update_target is None:
        return
    is_memory_update = memory_update_target is not None
    selected_target = memory_update_target if is_memory_update else target
    allowed_targets = MEMORY_UPDATE_TARGETS if is_memory_update else IMAGE_UPDATE_TARGETS
    if not isinstance(selected_target, str) or selected_target not in allowed_targets:
        operation = "memory" if is_memory_update else "image"
        raise StageTransitionError(
            f"{operation} update target is outside its closed service allowlist"
        )
    if is_memory_update and phase != "issued-connector-activation":
        raise StageTransitionError("memory updates require issued-connector-activation")
    if (
        not is_memory_update
        and target == GITHUB_ISSUES_IMAGE_UPDATE_TARGET
        and phase != "issued-connector-activation"
    ):
        raise StageTransitionError(
            "GitHub Issues image updates require issued-connector-activation and the existing connector runtime"
        )
    if (
        not is_memory_update
        and target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
        and phase not in ACCOUNT_CENTER_IMAGE_UPDATE_PHASES
    ):
        raise StageTransitionError(
            "Account Center image updates require the selected Graph bootstrap or issued activation phase"
        )
    if (
        not is_memory_update
        and target == HUGGINGFACE_IMAGE_UPDATE_TARGET
        and phase != "issued-connector-activation"
    ):
        raise StageTransitionError(
            "Hugging Face image updates require issued-connector-activation and the existing connector runtime"
        )
    if not is_memory_update and target == CLOUD_IMAGE_UPDATE_TARGET and phase not in PHASES:
        raise StageTransitionError(
            "Cloud Management image updates require provider-bootstrap or issued-connector-activation"
        )
    if (
        not is_memory_update
        and target == PERSISTENCE_IMAGE_UPDATE_TARGET
        and phase != "issued-connector-activation"
    ):
        raise StageTransitionError(
            "Persistence image updates require issued-connector-activation and the existing connector runtime"
        )
    if (
        not isinstance(selection_record, dict)
        or set(selection_record) != {"path", "sha256", "document"}
        or selection_record.get("path") != selection_path
        or not isinstance(selection_record.get("sha256"), str)
        or SHA256_RE.fullmatch(selection_record["sha256"]) is None
    ):
        raise StageTransitionError("image update requires an existing runtime selector v2")
    selection = selection_record.get("document")
    expected_selection_fields = {
        "schemaVersion",
        "project",
        "operationId",
        "planSha256",
        "phase",
        "composeFiles",
        "envFiles",
        "imageLock",
        "secretsDir",
        "configDir",
        "huggingfaceSeed",
        "runpodKey",
        "runtimeConfigIdentity",
        "updatedAtUtc",
    }
    if (
        not isinstance(selection, dict)
        or set(selection) != expected_selection_fields
        or selection.get("schemaVersion") != SELECTION_SCHEMA
        or selection.get("project") != project
        or selection.get("phase") != phase
        or not isinstance(selection.get("operationId"), str)
        or not selection["operationId"]
        or not isinstance(selection.get("updatedAtUtc"), str)
        or not isinstance(selection.get("planSha256"), str)
        or SHA256_RE.fullmatch(selection["planSha256"]) is None
    ):
        raise StageTransitionError("image update phase must match an existing runtime selector v2")
    if (
        active_config_dir != candidate_config_dir
        or selection.get("configDir") != active_config_dir
        or selection.get("secretsDir") != active_secrets_dir
    ):
        raise StageTransitionError(
            "image update must preserve selected config and secret directories"
        )
    if (
        selection.get("composeFiles") != active_inputs.get("composeFiles")
        or selection.get("envFiles") != active_inputs.get("envFiles")
        or selection.get("imageLock") != active_inputs.get("imageLock")
    ):
        raise StageTransitionError("active image update inputs differ from the runtime selector")
    if candidate_inputs.get("huggingfaceSeed") != selection.get(
        "huggingfaceSeed"
    ) or candidate_inputs.get("runpodKey") != selection.get("runpodKey"):
        raise StageTransitionError("image update must preserve the selected provider seed and key")
    if active_inputs.get("configDirectoryFiles") != candidate_inputs.get(
        "configDirectoryFiles"
    ) or active_inputs.get("secretFiles") != candidate_inputs.get("secretFiles"):
        raise StageTransitionError("image update must preserve config and secret file manifests")
    if is_memory_update and (
        candidate_inputs.get("envFiles") != active_inputs.get("envFiles")
        or candidate_inputs.get("imageLock") != active_inputs.get("imageLock")
    ):
        raise StageTransitionError("memory update must preserve selected env files and image lock")
    if (
        not isinstance(runtime_config_identity, dict)
        or set(runtime_config_identity) != {"active", "candidate"}
        or runtime_config_identity.get("active") != runtime_config_identity.get("candidate")
        or selection.get("runtimeConfigIdentity") != runtime_config_identity.get("active")
    ):
        raise StageTransitionError("image update must preserve every service mount identity")


def _validate_listener_repair_selection_contract(
    *,
    phase: str,
    project: str,
    selection_record: Any,
    selection_path: str,
    active_config_dir: str,
    candidate_config_dir: str,
    active_secrets_dir: str,
    active_inputs: dict[str, Any],
    candidate_inputs: dict[str, Any],
    runtime_config_identity: Any,
) -> None:
    """Bind command-only repair to the current bootstrap selector and all frozen inputs.

    中文：listener repair 只能替换 Compose 命令，运行配置、密钥、目录和镜像锁保持冻结。
    """

    selection = selection_record.get("document") if isinstance(selection_record, dict) else None
    expected_fields = {
        "schemaVersion",
        "project",
        "operationId",
        "planSha256",
        "phase",
        "composeFiles",
        "envFiles",
        "imageLock",
        "secretsDir",
        "configDir",
        "huggingfaceSeed",
        "runpodKey",
        "runtimeConfigIdentity",
        "updatedAtUtc",
    }
    if (
        phase != "provider-bootstrap"
        or not isinstance(selection_record, dict)
        or set(selection_record) != {"path", "sha256", "document"}
        or selection_record.get("path") != selection_path
        or not isinstance(selection_record.get("sha256"), str)
        or SHA256_RE.fullmatch(selection_record["sha256"]) is None
        or not isinstance(selection, dict)
        or set(selection) != expected_fields
        or selection.get("schemaVersion") != SELECTION_SCHEMA
        or selection.get("project") != project
        or selection.get("phase") != phase
        or not isinstance(selection.get("operationId"), str)
        or not selection["operationId"]
        or not isinstance(selection.get("updatedAtUtc"), str)
        or not isinstance(selection.get("planSha256"), str)
        or SHA256_RE.fullmatch(selection["planSha256"]) is None
    ):
        raise StageTransitionError(
            "listener repair requires the exact current provider-bootstrap selector v2"
        )
    if (
        active_config_dir != candidate_config_dir
        or selection.get("configDir") != active_config_dir
        or selection.get("secretsDir") != active_secrets_dir
    ):
        raise StageTransitionError(
            "listener repair must preserve the selected config and secret directories"
        )
    if (
        selection.get("composeFiles") != active_inputs.get("composeFiles")
        or selection.get("envFiles") != active_inputs.get("envFiles")
        or selection.get("imageLock") != active_inputs.get("imageLock")
        or active_inputs.get("envFiles") != candidate_inputs.get("envFiles")
        or active_inputs.get("imageLock", {}).get("sha256")
        != candidate_inputs.get("imageLock", {}).get("sha256")
    ):
        raise StageTransitionError("listener repair must preserve active env and image lock inputs")
    if (
        active_inputs.get("configDirectoryFiles") != candidate_inputs.get("configDirectoryFiles")
        or active_inputs.get("secretFiles") != candidate_inputs.get("secretFiles")
        or candidate_inputs.get("huggingfaceSeed") != selection.get("huggingfaceSeed")
        or candidate_inputs.get("runpodKey") != selection.get("runpodKey")
    ):
        raise StageTransitionError(
            "listener repair must preserve every config, secret, HF seed, and provider key"
        )
    if (
        not isinstance(runtime_config_identity, dict)
        or set(runtime_config_identity) != {"active", "candidate"}
        or runtime_config_identity.get("active") != runtime_config_identity.get("candidate")
        or selection.get("runtimeConfigIdentity") != runtime_config_identity.get("active")
    ):
        raise StageTransitionError("listener repair must preserve every service mount identity")


def _validate_runtime_config_identity(
    value: Any,
    *,
    expected_services: set[str],
    label: str,
) -> dict[str, list[dict[str, Any]]]:
    """Validate a closed service-to-mount identity map without accepting opaque fields."""

    if not isinstance(value, dict) or set(value) != expected_services:
        raise StageTransitionError(f"{label} service inventory is malformed")
    result: dict[str, list[dict[str, Any]]] = {}
    for service_name, mounts in value.items():
        if not isinstance(service_name, str) or not isinstance(mounts, list):
            raise StageTransitionError(f"{label} mount inventory is malformed")
        validated: list[dict[str, Any]] = []
        targets: set[str] = set()
        for row in mounts:
            if not isinstance(row, dict):
                raise StageTransitionError(f"{label} mount record is malformed")
            mount_type = row.get("type")
            target = row.get("target")
            common = {"type", "source", "target", "readOnly"}
            metadata_fields = {"sourceKind", "sourceUid", "sourceMode"}
            health_mount = target == AUTOMATION_HEALTH_TARGET
            allowed = common | (
                metadata_fields | {"contentSha256"} if mount_type == "bind" else set()
            )
            if mount_type == "bind" and health_mount:
                allowed.add("sourceGid")
            if set(row) - allowed or not common <= set(row):
                raise StageTransitionError(f"{label} mount record has unsupported fields")
            source = row.get("source")
            read_only = row.get("readOnly")
            if (
                mount_type not in {"bind", "volume"}
                or not isinstance(source, str)
                or not source
                or not isinstance(target, str)
                or not target.startswith("/")
                or not isinstance(read_only, bool)
                or target in targets
            ):
                raise StageTransitionError(f"{label} mount identity is malformed")
            targets.add(target)
            if mount_type == "bind":
                if not metadata_fields <= set(row):
                    raise StageTransitionError(f"{label} bind ownership is missing")
                if (
                    row.get("sourceKind") not in {"file", "directory"}
                    or isinstance(row.get("sourceUid"), bool)
                    or not isinstance(row.get("sourceUid"), int)
                    or isinstance(row.get("sourceMode"), bool)
                    or not isinstance(row.get("sourceMode"), int)
                    or not 0 <= row["sourceMode"] <= 0o7777
                ):
                    raise StageTransitionError(f"{label} bind ownership is malformed")
                if health_mount:
                    if (
                        not read_only
                        or row.get("sourceKind") != "directory"
                        or row.get("sourceUid") != 0
                        or isinstance(row.get("sourceGid"), bool)
                        or row.get("sourceGid") != 10001
                        or row.get("sourceMode") != 0o750
                        or "contentSha256" in row
                    ):
                        raise StageTransitionError(
                            f"{label} automation health mount identity is malformed"
                        )
                elif "sourceGid" in row:
                    raise StageTransitionError(f"{label} bind has unsupported group ownership")
                digest = row.get("contentSha256")
                needs_digest = read_only and not health_mount
                if needs_digest and (
                    not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None
                ):
                    raise StageTransitionError(f"{label} static bind content digest is missing")
                if digest is not None and (
                    not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None
                ):
                    raise StageTransitionError(f"{label} bind content digest is malformed")
                if not read_only and digest is not None:
                    raise StageTransitionError(f"{label} stateful bind must not hash its contents")
            validated.append(dict(row))
        if validated != sorted(
            validated, key=lambda row: (row["target"], row["type"], row["source"])
        ):
            raise StageTransitionError(f"{label} mount records are not canonical")
        result[service_name] = validated
    return result


def _verify_runtime_config_identity_sources(
    identity: dict[str, list[dict[str, Any]]], label: str
) -> None:
    """Recheck mount ownership and static read-only config content without hashing state."""

    for service_name, mounts in identity.items():
        for row in mounts:
            if row["type"] != "bind":
                continue
            path = Path(row["source"])
            if row["target"] == AUTOMATION_HEALTH_TARGET:
                metadata = _validate_automation_health_source(path, row["source"])
                if (
                    row.get("sourceKind") != "directory"
                    or row.get("sourceUid") != metadata.st_uid
                    or row.get("sourceGid") != metadata.st_gid
                    or row.get("sourceMode") != stat.S_IMODE(metadata.st_mode)
                    or "contentSha256" in row
                ):
                    raise StageTransitionError(
                        f"{label} {service_name} automation health mount identity changed"
                    )
                continue
            try:
                metadata = path.stat()
                resolved = str(path.resolve(strict=True))
            except OSError as error:
                raise StageTransitionError(
                    f"{label} {service_name} bind source is unavailable"
                ) from error
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise StageTransitionError(f"{label} {service_name} bind source type changed")
            kind = "directory" if stat.S_ISDIR(metadata.st_mode) else "file"
            if (
                resolved != row["source"]
                or kind != row["sourceKind"]
                or metadata.st_uid != row["sourceUid"]
                or stat.S_IMODE(metadata.st_mode) != row["sourceMode"]
            ):
                raise StageTransitionError(
                    f"{label} {service_name} bind ownership changed after selection"
                )
            digest = row.get("contentSha256")
            if digest is None:
                continue
            if kind == "file":
                current_digest = _sha256_file(path)
            else:
                contents = _directory_manifest(path, "Teams authorization config")
                current_digest = _sha256_bytes(_canonical_json(contents))
            if current_digest != digest:
                raise StageTransitionError(
                    f"{label} {service_name} static config source changed after selection"
                )


def _service_mounts_match(actual: Any, expected: list[dict[str, Any]]) -> bool:
    """Compare Docker's bind/volume mount read-back with one exact rendered service."""

    if not isinstance(actual, list):
        return False
    observed: list[dict[str, Any]] = []
    for row in actual:
        if not isinstance(row, dict) or row.get("Type") not in {"bind", "volume"}:
            continue
        target = row.get("Destination")
        source = row.get("Source") if row.get("Type") == "bind" else row.get("Name")
        read_write = row.get("RW")
        if (
            not isinstance(target, str)
            or not isinstance(source, str)
            or not isinstance(read_write, bool)
        ):
            return False
        observed.append(
            {
                "type": row["Type"],
                "source": source,
                "target": target,
                "readOnly": not read_write,
            }
        )
    expected_mounts = [
        {key: row[key] for key in ("type", "source", "target", "readOnly")} for row in expected
    ]
    return (
        sorted(observed, key=lambda row: (row["target"], row["type"], row["source"]))
        == expected_mounts
    )


def _selection_file_state(
    path: Path,
    active_inputs: dict[str, Any],
    *,
    project: str,
    config_dir: Path,
    secrets_dir: Path,
    active_service_names: set[str],
) -> dict[str, Any]:
    """Verify an existing stable runtime selection or record its first-run absence."""

    if path.is_symlink() or not path.is_absolute():
        raise StageTransitionError("runtime selection path cannot be a symlink")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.resolve(strict=True) != parent:
        raise StageTransitionError(
            "runtime selection parent must be an existing non-symlink directory"
        )
    parent_metadata = parent.stat()
    if parent_metadata.st_uid != os.geteuid() or stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        raise StageTransitionError("runtime selection parent must be current-user-owned mode 0700")
    if not path.exists():
        return {"path": str(path.resolve()), "sha256": None, "document": None}
    document = _read_json(path, "active runtime selection")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise StageTransitionError("active runtime selection must be owner-only mode 0600")
    expected_fields = {
        "schemaVersion",
        "project",
        "operationId",
        "planSha256",
        "phase",
        "composeFiles",
        "envFiles",
        "imageLock",
        "secretsDir",
        "configDir",
        "huggingfaceSeed",
        "runpodKey",
        "runtimeConfigIdentity",
        "updatedAtUtc",
    }
    if set(document) != expected_fields or document.get("schemaVersion") != SELECTION_SCHEMA:
        raise StageTransitionError("active runtime selection schema is unsupported or open")
    if (
        document.get("project") != project
        or document.get("configDir") != str(config_dir.resolve(strict=True))
        or document.get("secretsDir") != str(secrets_dir.resolve(strict=True))
    ):
        raise StageTransitionError("active runtime selection paths differ from explicit inputs")
    if (
        document.get("phase") not in PHASES
        or not isinstance(document.get("operationId"), str)
        or not isinstance(document.get("updatedAtUtc"), str)
        or not isinstance(document.get("planSha256"), str)
        or SHA256_RE.fullmatch(document["planSha256"]) is None
    ):
        raise StageTransitionError("active runtime selection identity is malformed")
    for field, key in (("composeFiles", "composeFiles"), ("envFiles", "envFiles")):
        if document.get(field) != active_inputs[key]:
            raise StageTransitionError(
                f"active runtime selection {field} differs from explicit inputs"
            )
    if document.get("imageLock") != active_inputs["imageLock"]:
        raise StageTransitionError(
            "active runtime selection image lock differs from explicit inputs"
        )
    _verify_record(document.get("huggingfaceSeed"), "active runtime selection HF seed")
    if document.get("runpodKey") is not None:
        _verify_record(document["runpodKey"], "active runtime selection RunPod key")
    mount_identity = _validate_runtime_config_identity(
        document.get("runtimeConfigIdentity"),
        expected_services=active_service_names,
        label="active runtime selection",
    )
    _verify_runtime_config_identity_sources(mount_identity, "active runtime selection")
    return {"path": str(path.resolve()), "sha256": _sha256_file(path), "document": document}


def _validate_output_directory(path: Path) -> None:
    """Require an owner-only artifact directory and an unused private plan path."""

    try:
        runtime_update._validate_plan_output_path(path)
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "stage transition plan output must be a new private artifact"
        ) from error


def _validate_private_directory(
    path: Path,
    label: str,
    *,
    require_artifact: bool = True,
    expected_uid: int | None = None,
) -> Path:
    """Validate one stable private directory without creating or changing it."""

    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError as error:
        raise StageTransitionError(f"private {label} directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != (os.geteuid() if expected_uid is None else expected_uid)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or resolved != path
        or (require_artifact and not preflight._private_artifact(path))
    ):
        raise StageTransitionError(
            f"private {label} directory must be a non-symlink owner-only directory"
        )
    return resolved


def _validate_config_directory_policy(
    *,
    image_update_target: str | None,
    memory_update_target: str | None,
    listener_repair: bool,
    active_config_dir: Path | str,
    candidate_config_dir: Path | str,
) -> None:
    """Keep normal transitions isolated and in-place update modes on the active config.

    中文：Graph bootstrap adds a private candidate config directory; image, memory, and
    listener-only updates continue to use the active directory.
    """

    try:
        active_path = Path(active_config_dir)
        candidate_path = Path(candidate_config_dir)
        if (
            not active_path.is_absolute()
            or not candidate_path.is_absolute()
            or active_path.resolve() != active_path
            or candidate_path.resolve() != candidate_path
        ):
            raise StageTransitionError(
                "transition config directories must be canonical absolute paths"
            )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise StageTransitionError("transition config directory inputs are malformed") from error

    in_place_update = (
        image_update_target is not None or memory_update_target is not None or listener_repair
    )
    if in_place_update and active_path != candidate_path:
        raise StageTransitionError("image update must keep the active config directory")
    if not in_place_update and active_path == candidate_path:
        raise StageTransitionError("candidate config directory must be separate from active config")


def _validate_active_candidate_configs(
    active_config_dir: Path,
    candidate_config_dir: Path,
    active: dict[str, Any],
    candidate: dict[str, Any],
    *,
    phase: str = "issued-connector-activation",
) -> None:
    """Keep live config sources immutable and allow only reviewed new provider files."""

    if active_config_dir.resolve(strict=True) == candidate_config_dir.resolve(strict=True):
        raise StageTransitionError(
            "candidate config directory must be versioned separately from active config"
        )
    active_configs = active.get("configs", {})
    candidate_configs = candidate.get("configs", {})
    office_graph_activation = (
        phase == "issued-connector-activation"
        and "microsoft-graph" in active.get("services", {})
        and "microsoft-graph" in candidate.get("services", {})
    )
    if not isinstance(active_configs, dict) or not isinstance(candidate_configs, dict):
        raise StageTransitionError("Compose config inventory is malformed")
    if not set(active_configs) <= set(candidate_configs):
        raise StageTransitionError("candidate removes existing Compose configs")
    for name, active_row in active_configs.items():
        candidate_row = candidate_configs.get(name)
        active_file = active_row.get("file") if isinstance(active_row, dict) else None
        candidate_file = candidate_row.get("file") if isinstance(candidate_row, dict) else None
        if not isinstance(active_file, str) or not isinstance(candidate_file, str):
            raise StageTransitionError(f"Compose config {name} is malformed")
        if Path(active_file).name != Path(candidate_file).name:
            raise StageTransitionError(f"candidate config {name} changes its canonical filename")
        active_digest = _sha256_file(Path(active_file))
        candidate_digest = _sha256_file(Path(candidate_file))
        changed_config_is_scoped_principal = (
            active_digest != candidate_digest
            and _is_recovered_persistence_principal_config(
                name=name,
                active_file=Path(active_file),
                candidate_file=Path(candidate_file),
                active_config_dir=active_config_dir,
                candidate_config_dir=candidate_config_dir,
                active=active,
                candidate=candidate,
            )
        )
        changed_config_is_runpod_notification = False
        changed_config_is_office_graph = False
        if active_digest != candidate_digest and name == "teams-credential-notifications":
            if office_graph_activation:
                changed_config_is_office_graph = True
            else:
                for config_name, expected_file in (
                    ("teams-credential-notifications", "teams-credential-notifications.json"),
                    ("connector-bindings", "connector-bindings.json"),
                ):
                    active_row = active_configs.get(config_name)
                    candidate_row = candidate_configs.get(config_name)
                    active_source = active_row.get("file") if isinstance(active_row, dict) else None
                    candidate_source = (
                        candidate_row.get("file") if isinstance(candidate_row, dict) else None
                    )
                    if (
                        not isinstance(active_source, str)
                        or not isinstance(candidate_source, str)
                        or Path(active_source).resolve(strict=True)
                        != (active_config_dir / expected_file).resolve(strict=True)
                        or Path(candidate_source).resolve(strict=True)
                        != (candidate_config_dir / expected_file).resolve(strict=True)
                    ):
                        raise StageTransitionError(
                            "RunPod notification update must use the canonical active/candidate config files"
                        )
                try:
                    preflight._validate_runpod_notification_config_delta(
                        active_config_dir,
                        candidate_config_dir,
                        candidate_compose=candidate,
                    )
                except preflight.PreflightError as error:
                    raise StageTransitionError(
                        "candidate notification config may add only one read-only RunPod connection"
                    ) from error
                changed_config_is_runpod_notification = True
        if (
            active_digest != candidate_digest
            and name
            not in {
                "navigator-principal",
                "connector-bindings",
                "connector-tool-catalog",
                "connector-bootstrap",
                "responsibility-targets",
            }
            and not changed_config_is_scoped_principal
            and not changed_config_is_runpod_notification
            and not changed_config_is_office_graph
            and not (
                office_graph_activation
                and name
                in {
                    "connector-bindings",
                    "connector-tool-catalog",
                }
            )
        ):
            raise StageTransitionError(f"candidate changes unrelated Compose config {name}")
    if office_graph_activation:
        _validate_office_graph_activation_config_delta(
            active_config_dir=active_config_dir,
            candidate_config_dir=candidate_config_dir,
            active=active,
            candidate=candidate,
        )
    added_configs = set(candidate_configs) - set(active_configs)
    expected_added = (
        {"teams-credential-notifications"}
        if "teams-credential-notifications" in candidate_configs
        and "teams-credential-notifications" not in active_configs
        else set()
    )
    if added_configs != expected_added:
        raise StageTransitionError(
            "candidate Compose config additions are outside the notification overlay"
        )


def _validate_office_graph_activation_config_delta(
    *,
    active_config_dir: Path,
    candidate_config_dir: Path,
    active: dict[str, Any],
    candidate: dict[str, Any],
) -> None:
    """Permit one read-only Graph binding/catalog and its owner notification connection."""

    def read_document(
        directory: Path,
        compose: dict[str, Any],
        filename: str,
        *,
        optional: bool = False,
    ) -> dict[str, Any] | None:
        config_name = filename.removesuffix(".json")
        config_row = compose.get("configs", {}).get(config_name, {})
        source = config_row.get("file") if isinstance(config_row, dict) else None
        path = directory / filename
        if optional and not path.exists():
            return None
        if not isinstance(source, str) or Path(source).resolve(strict=True) != path.resolve(
            strict=True
        ):
            raise StageTransitionError(
                f"Office Graph {filename} is not mounted from its private config directory"
            )
        return _read_json(path, f"Office Graph {filename}")

    active_bindings = read_document(active_config_dir, active, "connector-bindings.json")
    candidate_bindings = read_document(candidate_config_dir, candidate, "connector-bindings.json")
    active_catalog = read_document(active_config_dir, active, "connector-tool-catalog.json")
    candidate_catalog = read_document(
        candidate_config_dir, candidate, "connector-tool-catalog.json"
    )
    active_notification = read_document(
        active_config_dir, active, "teams-credential-notifications.json", optional=True
    )
    candidate_notification = read_document(
        candidate_config_dir, candidate, "teams-credential-notifications.json", optional=True
    )
    for name, document, field in (
        ("active bindings", active_bindings, "bindings"),
        ("candidate bindings", candidate_bindings, "bindings"),
        ("active catalog", active_catalog, "items"),
        ("candidate catalog", candidate_catalog, "items"),
    ):
        if (
            not isinstance(document, dict)
            or document.get("schemaVersion") != 1
            or not isinstance(document.get(field), list)
        ):
            raise StageTransitionError(f"Office Graph {name} document is malformed")

    def keyed(rows: list[Any], key: str, label: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            value = row.get(key) if isinstance(row, dict) else None
            if not isinstance(value, str) or not value or value in result:
                raise StageTransitionError(
                    f"Office Graph {label} contains duplicate or missing IDs"
                )
            result[value] = row
        return result

    old_bindings = keyed(active_bindings["bindings"], "bindingId", "active bindings")
    new_bindings = keyed(candidate_bindings["bindings"], "bindingId", "candidate bindings")
    graph_bindings = [
        row for row in new_bindings.values() if row.get("providerId") == "microsoft_graph"
    ]
    if (
        len(graph_bindings) != 1
        or any(row.get("providerId") == "microsoft_graph" for row in old_bindings.values())
        or {key: value for key, value in new_bindings.items() if key in old_bindings}
        != old_bindings
        or set(new_bindings) - set(old_bindings) != {graph_bindings[0]["bindingId"]}
    ):
        raise StageTransitionError("Office Graph activation may add only one issued Graph binding")
    graph_binding = graph_bindings[0]
    graph_connection_ref = graph_binding.get("connectionRef")
    if not isinstance(graph_connection_ref, str):
        raise StageTransitionError("Office Graph binding connection reference is malformed")

    old_catalog = keyed(active_catalog["items"], "bindingId", "active catalog")
    new_catalog = keyed(candidate_catalog["items"], "bindingId", "candidate catalog")
    if {
        key: value for key, value in new_catalog.items() if key in old_catalog
    } != old_catalog or set(new_catalog) - set(old_catalog) != {graph_binding["bindingId"]}:
        raise StageTransitionError("Office Graph activation changes catalog items beyond Graph")

    if candidate_notification is None:
        raise StageTransitionError(
            "Office Graph activation requires owner credential notification scope"
        )
    if active_notification is not None:
        if {
            key: value for key, value in candidate_notification.items() if key != "connections"
        } != {key: value for key, value in active_notification.items() if key != "connections"}:
            raise StageTransitionError(
                "Office Graph activation changes the owner notification route"
            )
        old_connections = keyed(
            active_notification.get("connections", []), "provider", "active notification"
        )
        new_connections = keyed(
            candidate_notification.get("connections", []), "provider", "candidate notification"
        )
        if (
            {key: value for key, value in new_connections.items() if key in old_connections}
            != old_connections
            or set(new_connections) - set(old_connections) != {"microsoft_graph"}
            or new_connections["microsoft_graph"].get("connectionId") != graph_connection_ref
        ):
            raise StageTransitionError(
                "Office Graph notification update may add only its issued connection"
            )
    else:
        new_connections = keyed(
            candidate_notification.get("connections", []), "provider", "candidate notification"
        )
        if (
            "microsoft_graph" not in new_connections
            or new_connections["microsoft_graph"].get("connectionId") != graph_connection_ref
        ):
            raise StageTransitionError("Office Graph notification lacks its issued connection")
    _validate_office_graph_owner_health_principal_delta(
        active_config_dir=active_config_dir,
        candidate_config_dir=candidate_config_dir,
        active=active,
        candidate=candidate,
    )


def _validate_office_graph_owner_health_principal_delta(
    *,
    active_config_dir: Path,
    candidate_config_dir: Path,
    active: dict[str, Any],
    candidate: dict[str, Any],
) -> None:
    """Allow only the Directory owner's credential-health publishing grant."""

    def read_principal(
        compose: dict[str, Any], directory: Path, label: str
    ) -> tuple[str, dict[str, Any]]:
        aliases = [
            name
            for name in compose.get("configs", {})
            if _recovered_principal_config_consumers(compose, name)
            == [("navigator-persistence", NAVIGATOR_PRINCIPAL_CONFIG_TARGET)]
        ]
        if len(aliases) != 1:
            raise StageTransitionError(f"Office Graph {label} principal config alias is ambiguous")
        alias = aliases[0]
        row = compose["configs"].get(alias, {})
        source = row.get("file") if isinstance(row, dict) else None
        expected_path = directory / NAVIGATOR_PRINCIPAL_CONFIG_FILENAME
        if not isinstance(source, str) or Path(source).resolve(
            strict=True
        ) != expected_path.resolve(strict=True):
            raise StageTransitionError(
                f"Office Graph {label} principal config is outside its private version"
            )
        document = _read_json(expected_path, f"Office Graph {label} principal config")
        if not isinstance(document, dict) or not isinstance(document.get("principals"), list):
            raise StageTransitionError(f"Office Graph {label} principal config is malformed")
        return alias, document

    active_alias, old = read_principal(active, active_config_dir, "active")
    candidate_alias, new = read_principal(candidate, candidate_config_dir, "candidate")
    if active_alias != candidate_alias:
        raise StageTransitionError("Office Graph activation changes the principal config alias")
    active_actor_map = _read_json(
        active_config_dir / "work-actor-token-map.json", "active Graph Work actor map"
    )
    candidate_actor_map = _read_json(
        candidate_config_dir / "work-actor-token-map.json", "candidate Graph Work actor map"
    )
    if active_actor_map != candidate_actor_map:
        raise StageTransitionError("Office Graph activation changes its Directory actor map")
    actor_rows = active_actor_map.get("actors")
    writer_slots = [f"CYRENE_WORK_ACTOR_TOKEN_{index:02d}" for index in range(1, 6)]
    if (
        not isinstance(actor_rows, list)
        or not 1 <= len(actor_rows) <= 5
        or any(not isinstance(row, dict) for row in actor_rows)
        or [row.get("tokenEnv") for row in actor_rows] != writer_slots[: len(actor_rows)]
    ):
        raise StageTransitionError(
            "Office Graph activation actor map must retain contiguous owner-first slots"
        )
    writer_slots = writer_slots[: len(actor_rows)]
    old_rows = old.get("principals")
    new_rows = new.get("principals")
    old_by_token = {row.get("token_env"): row for row in old_rows if isinstance(row, dict)}
    new_by_token = {row.get("token_env"): row for row in new_rows if isinstance(row, dict)}
    expected_tokens = {
        "NAVIGATOR_SESSION_TOKEN",
        "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN",
        *writer_slots,
    }
    if (
        len(old_by_token) != len(old_rows)
        or len(new_by_token) != len(new_rows)
        or set(old_by_token) != expected_tokens
        or set(new_by_token) != expected_tokens
    ):
        raise StageTransitionError(
            "Office Graph activation must preserve the selected Work principals"
        )
    owner = (
        candidate.get("services", {})
        .get("directory-operator", {})
        .get("environment", {})
        .get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID")
    )
    old_session = old_by_token["NAVIGATOR_SESSION_TOKEN"]
    new_session = new_by_token["NAVIGATOR_SESSION_TOKEN"]
    if (
        old_session.get("actor_id") != owner
        or new_session.get("actor_id") != owner
        or new_session.get("can_publish_credential_health") is not True
    ):
        raise StageTransitionError(
            "Office Graph activation must grant health publishing only to the owner session"
        )
    normalized_new = copy.deepcopy(new)
    normalized_session = next(
        row
        for row in normalized_new["principals"]
        if row.get("token_env") == "NAVIGATOR_SESSION_TOKEN"
    )
    if "can_publish_credential_health" in old_session:
        normalized_session["can_publish_credential_health"] = old_session[
            "can_publish_credential_health"
        ]
    else:
        normalized_session.pop("can_publish_credential_health", None)
    if normalized_new != old:
        raise StageTransitionError(
            "Office Graph activation changes principal grants beyond owner health publishing"
        )


def _recovered_principal_config_consumers(
    compose: dict[str, Any], config_name: str
) -> list[tuple[str, Any]]:
    """Return every service/target consuming one Compose config alias."""

    services = compose.get("services")
    if not isinstance(services, dict):
        raise StageTransitionError("Compose config consumers have no service inventory")
    consumers: list[tuple[str, Any]] = []
    for service_name, service in services.items():
        if not isinstance(service_name, str) or not isinstance(service, dict):
            raise StageTransitionError("Compose config consumer service is malformed")
        for row in _mount_rows(service, "configs"):
            if row.get("source") == config_name:
                consumers.append((service_name, row.get("target")))
    return consumers


def _is_recovered_persistence_principal_config(
    *,
    name: str,
    active_file: Path,
    candidate_file: Path,
    active_config_dir: Path,
    candidate_config_dir: Path,
    active: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    """Scope the recovery alias exception to the one Persistence principal mount.

    中文：仅允许已存在的恢复别名切换同名 owner principal 文件，不扩展其他 config alias。
    """

    if name != RECOVERED_PERSISTENCE_PRINCIPAL_CONFIG:
        return False
    if (
        active_file.name != NAVIGATOR_PRINCIPAL_CONFIG_FILENAME
        or candidate_file.name != NAVIGATOR_PRINCIPAL_CONFIG_FILENAME
        or active_file.resolve(strict=True).parent != active_config_dir.resolve(strict=True)
        or candidate_file.resolve(strict=True).parent != candidate_config_dir.resolve(strict=True)
    ):
        return False
    expected_consumer = [("navigator-persistence", NAVIGATOR_PRINCIPAL_CONFIG_TARGET)]
    return (
        _recovered_principal_config_consumers(active, name) == expected_consumer
        and _recovered_principal_config_consumers(candidate, name) == expected_consumer
    )


def _validate_active_candidate_secrets(active: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Preserve active host-secret sources except the explicit HF seed and RunPod key inputs."""

    active_secrets = active.get("secrets", {})
    candidate_secrets = candidate.get("secrets", {})
    if not isinstance(active_secrets, dict) or not isinstance(candidate_secrets, dict):
        raise StageTransitionError("Compose secret inventory is malformed")
    if not set(active_secrets) <= set(candidate_secrets):
        raise StageTransitionError("candidate removes an existing Compose secret")
    allowed_additions = (
        preflight.HUGGINGFACE_OPTIONAL_SECRET_NAMES
        | {
            "cloud-management-exchange-hmac-key",
            "gcp-wif-credential-config",
            "runpod-api-key",
            "account-center-graph-provider-hmac-key",
            "account-center-microsoft-graph-callback-hmac-key",
            "account-center-microsoft-graph-client-secret",
            "exchange-graph-auth-context-hmac-key",
            "microsoft-graph-server-cert",
            "microsoft-graph-server-key",
            "microsoft-graph-health-client-cert",
            "microsoft-graph-health-client-key",
            "microsoft-graph-account-center-client-cert",
            "microsoft-graph-account-center-client-key",
        }
        | OFFICE_PERSONAL_ROUTE_SECRET_NAMES
        | CLOUD_MANAGEMENT_TLS_SECRET_NAMES
    )
    added = set(candidate_secrets) - set(active_secrets)
    if not added <= allowed_additions:
        raise StageTransitionError("candidate adds a secret outside the HF/cloud provider set")
    for name, active_row in active_secrets.items():
        candidate_row = candidate_secrets.get(name)
        old_file = active_row.get("file") if isinstance(active_row, dict) else None
        new_file = candidate_row.get("file") if isinstance(candidate_row, dict) else None
        if not isinstance(old_file, str) or not isinstance(new_file, str):
            raise StageTransitionError(f"Compose secret {name} is malformed")
        if Path(old_file).name != Path(new_file).name:
            raise StageTransitionError(f"candidate secret {name} changes its canonical filename")
        if name not in {"account-center-huggingface-oauth-source", "runpod-api-key"} and str(
            Path(old_file).resolve(strict=True)
        ) != str(Path(new_file).resolve(strict=True)):
            raise StageTransitionError(f"candidate changes unrelated secret source {name}")
    _validate_cloud_management_tls_secret_consumers(candidate)


def _validate_cloud_management_tls_secret_consumers(candidate: dict[str, Any]) -> None:
    """Keep the four new mTLS secret sources exclusive to Cloud Management."""

    services = candidate.get("services")
    if not isinstance(services, dict):
        raise StageTransitionError(
            "candidate service inventory is malformed for Cloud Management TLS"
        )
    uses: dict[str, list[tuple[str, Any]]] = {
        name: [] for name in CLOUD_MANAGEMENT_TLS_SECRET_NAMES
    }
    for service_name, service in services.items():
        if not isinstance(service_name, str) or not isinstance(service, dict):
            raise StageTransitionError(
                "candidate service entry is malformed for Cloud Management TLS"
            )
        for row in _mount_rows(service, "secrets"):
            source_name = row.get("source")
            if isinstance(source_name, str) and source_name in CLOUD_MANAGEMENT_TLS_SECRET_NAMES:
                uses[source_name].append((service_name, row.get("target")))

    if any(
        uses[name] != [("cloud-management-phase2", name)]
        for name in CLOUD_MANAGEMENT_TLS_SECRET_NAMES
    ):
        raise StageTransitionError(
            "Cloud Management mTLS secrets must be mounted once each, only to their canonical Cloud Management targets"
        )


def _validate_compose_definition_deltas(active: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Allow only the named provider overlay network, volume, secret, and config additions."""

    for field, allowed_additions in (
        (
            "networks",
            {
                "huggingface-exchange-private",
                "huggingface-account-center-private",
                "huggingface-egress",
                "cloud-management-egress",
                "graph-exchange-private",
                "graph-account-center-private",
                "graph-egress",
            },
        ),
        ("volumes", {"cloud-management-state"}),
    ):
        old_rows = active.get(field, {})
        new_rows = candidate.get(field, {})
        if not isinstance(old_rows, dict) or not isinstance(new_rows, dict):
            raise StageTransitionError(f"Compose {field} definition inventory is malformed")
        if not set(old_rows) <= set(new_rows):
            raise StageTransitionError(f"candidate removes an existing Compose {field} definition")
        if any(new_rows[name] != old_rows[name] for name in old_rows):
            raise StageTransitionError(f"candidate changes an existing Compose {field} definition")
        if not (set(new_rows) - set(old_rows)) <= allowed_additions:
            raise StageTransitionError(f"candidate adds an unsupported Compose {field} definition")

    for field in ("configs", "secrets"):
        old_rows = active.get(field, {})
        new_rows = candidate.get(field, {})
        if not isinstance(old_rows, dict) or not isinstance(new_rows, dict):
            raise StageTransitionError(f"Compose {field} inventory is malformed")
        if not set(old_rows) <= set(new_rows):
            raise StageTransitionError(f"candidate removes an existing Compose {field}")


def _validate_candidate_secret_paths(
    *,
    candidate: dict[str, Any],
    active_secrets_dir: Path,
    huggingface_seed_file: Path,
    runpod_key_file: Path | None,
    phase: str,
) -> None:
    """Bind configured staged credential paths to exact immutable candidate inputs."""

    if phase not in PHASES:
        raise StageTransitionError("transition phase is invalid")
    if candidate.get("secrets", {}).get("account-center-huggingface-oauth-source", {}).get(
        "file"
    ) != str(huggingface_seed_file.resolve(strict=True)):
        raise StageTransitionError(
            "candidate Account Center OAuth mount differs from the explicit HF seed"
        )
    cloud = candidate.get("services", {}).get("cloud-management-phase2", {})
    cloud_profiles = cloud.get("profiles", []) if isinstance(cloud, dict) else []
    runpod_enabled = "runpod" in cloud_profiles
    runpod_secret = candidate.get("secrets", {}).get("runpod-api-key")
    runpod_environment = cloud.get("environment", {}) if isinstance(cloud, dict) else {}
    if runpod_enabled:
        if (
            runpod_key_file is None
            or not isinstance(runpod_secret, dict)
            or runpod_secret.get("file") != str(runpod_key_file.resolve(strict=True))
            or runpod_environment.get("CYRENE_CLOUD_MGMT_RUNPOD_API_KEY_FILE")
            != "/run/secrets/runpod-api-key"
        ):
            raise StageTransitionError(
                "candidate RunPod profile requires its explicit mounted API key file"
            )
    elif runpod_key_file is not None or runpod_secret is not None:
        raise StageTransitionError("an unconfigured RunPod profile must not have a candidate key")
    candidate_credentials = [("HF seed", huggingface_seed_file)]
    if runpod_enabled:
        candidate_credentials.append(("RunPod key", runpod_key_file))
    for name, path in candidate_credentials:
        if path is None:
            raise StageTransitionError(f"candidate {name} path is unavailable")
        if path.is_symlink() or not path.is_file() or not preflight._private_artifact(path):
            raise StageTransitionError(f"candidate {name} must be a regular private artifact")
        metadata = path.stat()
        if (
            metadata.st_uid != 10001
            or metadata.st_mode & 0o077
            or stat.S_IMODE(metadata.st_mode) != 0o400
        ):
            raise StageTransitionError(f"candidate {name} must be owned by UID 10001 and mode 0400")
    if not active_secrets_dir.is_dir() or active_secrets_dir.is_symlink():
        raise StageTransitionError("active shared secrets directory is unavailable")


def _live_service_inventory(
    *,
    compose: dict[str, Any],
    image_lock: dict[str, Any],
    docker_prefix: list[str],
    mount_identity_by_service: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Inspect exact running project containers and reject duplicates or unmanaged workers."""

    project = compose.get("name")
    if not isinstance(project, str) or not project:
        raise StageTransitionError("Compose project name is missing")
    expected = _runtime_service_inventory(compose)
    expected_mount_identity = (
        _runtime_config_identity_for_compose(compose)
        if mount_identity_by_service is None
        else _validate_runtime_config_identity(
            mount_identity_by_service,
            expected_services=set(expected),
            label="runtime configuration identity",
        )
    )
    _verify_runtime_config_identity_sources(expected_mount_identity, "runtime configuration")
    locked_ids = _lock_image_ids(image_lock)
    office_oauth_setup_enabled = _office_oauth_setup_bridge_enabled(compose)
    output = runtime_update._checked_stdout(
        [
            *docker_prefix,
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.ID}}",
        ],
        label="stage transition active service inventory",
    )
    ids = [row.strip() for row in output.splitlines() if row.strip()]
    if not ids or any(runtime_update.CONTAINER_ID_RE.fullmatch(item) is None for item in ids):
        raise StageTransitionError("Docker returned an empty or malformed project inventory")
    rows = runtime_update._decode_json_output(
        runtime_update._checked_stdout(
            [*docker_prefix, "inspect", *ids], label="stage transition service inspection"
        ),
        "stage transition service inspection",
    )
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise StageTransitionError("Docker returned an incomplete project inspection")
    by_service: dict[str, dict[str, Any]] = {}
    published_container_ids: list[str] = []
    office_oauth_container_ids: list[str] = []
    published_values: set[str] = set()
    office_oauth_network_ports: Any = None
    office_oauth_networks: Any = None
    for row in rows:
        config_row = row.get("Config")
        labels = config_row.get("Labels") if isinstance(config_row, dict) else None
        state = row.get("State")
        service_name = (
            labels.get("com.docker.compose.service") if isinstance(labels, dict) else None
        )
        if not isinstance(labels, dict) or labels.get("com.docker.compose.project") != project:
            raise StageTransitionError("running/stopped project container has an unknown owner")
        if service_name not in expected:
            raise StageTransitionError("project contains an unexpected runtime service container")
        if service_name in by_service:
            raise StageTransitionError(f"project service {service_name} has multiple containers")
        if (
            not isinstance(state, dict)
            or state.get("Running") is not True
            or state.get("Paused") is True
        ):
            raise StageTransitionError(f"project service {service_name} is not running")
        image_ref = config_row.get("Image") if isinstance(config_row, dict) else None
        image_id = row.get("Image")
        expected_image_ref = expected[service_name].get("image")
        if (
            image_ref != expected_image_ref
            or not isinstance(image_id, str)
            or image_id != locked_ids.get(expected_image_ref)
        ):
            raise StageTransitionError(
                f"project service {service_name} differs from its exact active image lock"
            )
        health_record = state.get("Health")
        health = health_record.get("Status") if isinstance(health_record, dict) else "running"
        if expected[service_name].get("healthcheck") and health != "healthy":
            raise StageTransitionError(f"project service {service_name} is not healthy")
        networks_record = row.get("NetworkSettings")
        networks = networks_record.get("Networks") if isinstance(networks_record, dict) else None
        if not isinstance(networks, dict) or not networks:
            raise StageTransitionError(f"project service {service_name} has no network inventory")
        expected_mounts = expected_mount_identity[service_name]
        actual_mounts = row.get("Mounts")
        if not _service_mounts_match(actual_mounts, expected_mounts):
            raise StageTransitionError(
                f"project service {service_name} mounted config/volume sources differ from the selected runtime identity"
            )
        container_id = row.get("Id")
        if (
            not isinstance(container_id, str)
            or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None
        ):
            raise StageTransitionError(
                f"project service {service_name} has a malformed container ID"
            )
        by_service[service_name] = {
            "containerId": container_id,
            "imageRef": image_ref,
            "imageId": image_id,
            "health": health,
            "networkNames": sorted(networks),
            "mounts": expected_mounts,
        }
        if expected[service_name].get("ports"):
            host_config = row.get("HostConfig")
            bindings = host_config.get("PortBindings") if isinstance(host_config, dict) else None
            expected_port_rows = expected[service_name].get("ports", [])
            expected_bindings = {
                f"{item.get('target')}/{item.get('protocol', 'tcp')}": [
                    {
                        "HostIp": item.get("host_ip") or "0.0.0.0",
                        "HostPort": str(item.get("published", "")),
                    }
                ]
                for item in expected_port_rows
                if isinstance(item, dict)
            }
            if not isinstance(bindings, dict) or bindings != expected_bindings:
                raise StageTransitionError(
                    "active Teams ingress host port binding differs from Compose"
                )
            if office_oauth_setup_enabled and service_name == "exchange":
                office_oauth_container_ids.append(row["Id"])
                office_oauth_network_ports = (
                    networks_record.get("Ports") if isinstance(networks_record, dict) else None
                )
                office_oauth_networks = networks
            else:
                published_container_ids.append(row["Id"])
                published_values.update(
                    str(item.get("published", ""))
                    for item in expected_port_rows
                    if isinstance(item, dict)
                )
    if set(by_service) != set(expected):
        missing = sorted(set(expected) - set(by_service))
        raise StageTransitionError(
            "project live service inventory is incomplete: " + ", ".join(missing)
        )
    if published_container_ids or office_oauth_setup_enabled:
        if (
            len(published_container_ids) != 1
            or len(published_values) != 1
            or not next(iter(published_values)).isdigit()
        ):
            raise StageTransitionError(
                "active Teams ingress must have one exact published TCP port"
            )
        published_port = next(iter(published_values))
        published = runtime_update._checked_stdout(
            [
                *docker_prefix,
                "ps",
                "--filter",
                f"publish={published_port}",
                "--format",
                "{{.ID}}",
            ],
            label="Teams ingress published port ownership",
        )
        port_ids = [value.strip() for value in published.splitlines() if value.strip()]
        if len(published_container_ids) != 1 or len(port_ids) != 1:
            raise StageTransitionError(
                f"published TCP {published_port} is not uniquely owned by the active project ingress"
            )
        if not port_ids[0].startswith(published_container_ids[0][:12]):
            raise StageTransitionError(f"published TCP {published_port} has a non-project owner")
    if office_oauth_setup_enabled:
        office_port = str(preflight.OFFICE_OAUTH_SETUP_PORT)
        if len(office_oauth_container_ids) != 1:
            raise StageTransitionError("Exchange Office OAuth port has no unique project container")
        office_port_key = f"{office_port}/tcp"
        if (
            not isinstance(office_oauth_network_ports, dict)
            or not office_oauth_network_ports
            or office_port_key not in office_oauth_network_ports
            or any(
                not isinstance(key, str)
                or re.fullmatch(r"[1-9][0-9]{0,4}/(?:tcp|udp|sctp)", key) is None
                or int(key.split("/", 1)[0]) > 65535
                for key in office_oauth_network_ports
            )
        ):
            raise StageTransitionError("Exchange Office OAuth NetworkSettings.Ports is unknown")
        office_network_binding = office_oauth_network_ports[office_port_key]
        published = runtime_update._checked_stdout(
            [
                *docker_prefix,
                "ps",
                "--filter",
                f"publish={office_port}",
                "--format",
                "{{.ID}}",
            ],
            label="Exchange Office OAuth published port ownership",
        )
        port_ids = [value.strip() for value in published.splitlines() if value.strip()]
        if office_network_binding is None:
            if any(binding is not None for binding in office_oauth_network_ports.values()):
                raise StageTransitionError(
                    "unpublished Exchange Office OAuth bridge has an actual port publication"
                )
            _validate_office_oauth_internal_networks(
                office_oauth_networks, docker_prefix=docker_prefix
            )
            if port_ids:
                raise StageTransitionError(
                    f"internal Exchange Office OAuth TCP {office_port} was published by Docker"
                )
        else:
            if (
                not isinstance(office_network_binding, list)
                or len(office_network_binding) != 1
                or not isinstance(office_network_binding[0], dict)
                or set(office_network_binding[0]) != {"HostIp", "HostPort"}
                or office_network_binding[0].get("HostIp") != "127.0.0.1"
                or office_network_binding[0].get("HostPort") != office_port
                or any(
                    key != office_port_key and binding is not None
                    for key, binding in office_oauth_network_ports.items()
                )
            ):
                raise StageTransitionError(
                    "published Exchange Office OAuth TCP must be its exact loopback binding"
                )
            if len(port_ids) != 1:
                raise StageTransitionError(
                    f"published Exchange Office OAuth TCP {office_port} is not uniquely owned"
                )
            if not port_ids[0].startswith(office_oauth_container_ids[0][:12]):
                raise StageTransitionError(
                    f"published Exchange Office OAuth TCP {office_port} has a non-Exchange owner"
                )
    return by_service


def _inspect_exact_service(
    *,
    project: str,
    service_name: str,
    expected_image: dict[str, str],
    has_healthcheck: bool,
    docker_prefix: list[str],
    allow_starting: bool = False,
) -> dict[str, Any]:
    """Read one exact target container after a recorded Compose mutation."""

    output = runtime_update._checked_stdout(
        [
            *docker_prefix,
            "ps",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service_name}",
            "--format",
            "{{.ID}}",
        ],
        label=f"{service_name} stage transition readback",
    )
    ids = [value.strip() for value in output.splitlines() if value.strip()]
    if len(ids) != 1 or runtime_update.CONTAINER_ID_RE.fullmatch(ids[0]) is None:
        raise StageTransitionError(
            f"{service_name} has no unique project-owned container after apply"
        )
    rows = runtime_update._decode_json_output(
        runtime_update._checked_stdout(
            [*docker_prefix, "inspect", ids[0]], label=f"{service_name} target inspection"
        ),
        f"{service_name} target inspection",
    )
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise StageTransitionError(f"{service_name} inspection is ambiguous")
    row = rows[0]
    container_config = row.get("Config")
    labels = container_config.get("Labels") if isinstance(container_config, dict) else None
    state = row.get("State")
    if (
        not isinstance(labels, dict)
        or labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != service_name
        or not isinstance(state, dict)
        or state.get("Running") is not True
        or state.get("Paused") is True
        or row.get("Image") != expected_image.get("imageId")
        or container_config.get("Image") != expected_image.get("imageRef")
    ):
        raise StageTransitionError(f"{service_name} did not start its exact candidate image")
    health_record = state.get("Health")
    health = health_record.get("Status") if isinstance(health_record, dict) else "running"
    if has_healthcheck and health != "healthy" and not allow_starting:
        raise StageTransitionError(f"{service_name} has not passed its health check")
    networks_record = row.get("NetworkSettings")
    networks = networks_record.get("Networks") if isinstance(networks_record, dict) else None
    if not isinstance(networks, dict) or not networks:
        raise StageTransitionError(f"{service_name} has no active network inventory")
    container_id = row.get("Id")
    if not isinstance(container_id, str) or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError(f"{service_name} returned a malformed container ID")
    return {
        "containerId": container_id,
        "imageRef": expected_image["imageRef"],
        "imageId": expected_image["imageId"],
        "health": health,
        "networkNames": sorted(networks),
    }


def _wait_for_target_service(
    *,
    project: str,
    service_name: str,
    expected_image: dict[str, str],
    has_healthcheck: bool,
    timeout_seconds: int,
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Poll read-only container state until the exact target is healthy or times out."""

    deadline = time.monotonic() + timeout_seconds
    while True:
        current = _inspect_exact_service(
            project=project,
            service_name=service_name,
            expected_image=expected_image,
            has_healthcheck=has_healthcheck,
            docker_prefix=docker_prefix,
            allow_starting=True,
        )
        if current["health"] == "unhealthy":
            raise StageTransitionError(f"{service_name} became unhealthy during candidate startup")
        if current["health"] == "healthy" or (
            not has_healthcheck and current["health"] == "running"
        ):
            return current
        if time.monotonic() >= deadline:
            raise StageTransitionError(
                f"{service_name} did not become healthy before the bounded timeout"
            )
        time.sleep(2)


def _validate_sqlite_report_shape(report: Any, label: str) -> dict[str, Any]:
    """Validate the bounded SQLite schema/count/hash readback without exposing row values."""

    if not isinstance(report, dict):
        raise StageTransitionError(f"{label} is malformed")
    if (
        report.get("integrity") != "ok"
        or isinstance(report.get("userVersion"), bool)
        or not isinstance(report.get("userVersion"), int)
        or report["userVersion"] < 0
        or not isinstance(report.get("schemaSha256"), str)
        or SHA256_RE.fullmatch(report["schemaSha256"]) is None
        or not isinstance(report.get("objects"), list)
        or any(not isinstance(name, str) for name in report["objects"])
        or not isinstance(report.get("schemaObjects"), list)
        or not isinstance(report.get("tables"), dict)
    ):
        raise StageTransitionError(f"{label} is malformed")
    schema_objects = report["schemaObjects"]
    if any(
        not isinstance(row, dict)
        or set(row) != {"type", "name", "sql"}
        or row.get("type") not in {"table", "index", "view", "trigger"}
        or not isinstance(row.get("name"), str)
        or not row["name"]
        or (row.get("sql") is not None and not isinstance(row.get("sql"), str))
        for row in schema_objects
    ):
        raise StageTransitionError(f"{label} schema-object records are malformed")
    expected_objects = sorted(f"{row['type']}:{row['name']}" for row in schema_objects)
    if (
        report["objects"] != expected_objects
        or _sha256_bytes(_canonical_json(schema_objects)) != report["schemaSha256"]
    ):
        raise StageTransitionError(f"{label} schema digest does not match its object inventory")
    table_names = {row["name"] for row in schema_objects if row["type"] == "table"}
    tables = report["tables"]
    if set(tables) != table_names:
        raise StageTransitionError(f"{label} table inventory differs from sqlite_master")
    for name, row in tables.items():
        required = {
            "columns",
            "createSql",
            "primaryKeyColumns",
            "foreignKeys",
            "checkConstraints",
            "uniqueIndexes",
            "rowCount",
            "rowsDigest",
        }
        optional = {"emptyOrganizationIdRows", "preservedRowsCount", "preservedRowsDigest"}
        if (
            not isinstance(name, str)
            or not isinstance(row, dict)
            or set(row) - required - optional
            or not required <= set(row)
            or not isinstance(row["columns"], list)
            or (row["createSql"] is not None and not isinstance(row["createSql"], str))
            or not isinstance(row["primaryKeyColumns"], list)
            or not isinstance(row["foreignKeys"], list)
            or not isinstance(row["checkConstraints"], list)
            or not isinstance(row["uniqueIndexes"], list)
            or isinstance(row["rowCount"], bool)
            or not isinstance(row["rowCount"], int)
            or row["rowCount"] < 0
            or not isinstance(row["rowsDigest"], str)
            or SHA256_RE.fullmatch(row["rowsDigest"]) is None
        ):
            raise StageTransitionError(f"{label} table report is malformed")
        if any(
            not isinstance(column, dict)
            or set(column) != {"name", "type", "notNull", "default", "primaryKeyOrdinal", "hidden"}
            or not isinstance(column.get("name"), str)
            or not isinstance(column.get("type"), str)
            or not isinstance(column.get("notNull"), bool)
            or isinstance(column.get("primaryKeyOrdinal"), bool)
            or not isinstance(column.get("primaryKeyOrdinal"), int)
            or isinstance(column.get("hidden"), bool)
            or not isinstance(column.get("hidden"), int)
            for column in row["columns"]
        ):
            raise StageTransitionError(f"{label} table column report is malformed")
        schema_sql = next(
            (
                item["sql"]
                for item in schema_objects
                if item["type"] == "table" and item["name"] == name
            ),
            None,
        )
        if row["createSql"] != schema_sql:
            raise StageTransitionError(f"{label} table DDL differs from sqlite_master")
        for count_name in ("emptyOrganizationIdRows", "preservedRowsCount"):
            if count_name in row and (
                isinstance(row[count_name], bool)
                or not isinstance(row[count_name], int)
                or row[count_name] < 0
            ):
                raise StageTransitionError(f"{label} table reconciliation count is malformed")
        if "preservedRowsDigest" in row and (
            not isinstance(row["preservedRowsDigest"], str)
            or SHA256_RE.fullmatch(row["preservedRowsDigest"]) is None
        ):
            raise StageTransitionError(f"{label} table reconciliation digest is malformed")
    return report


def _live_sqlite_report(
    *,
    docker_prefix: list[str],
    container_id: str,
    database_path: str,
    comparison_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stream live SQLite table hashes plus the snapshot column contract for comparison."""

    comparison_json = json.dumps(
        comparison_contract or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    script = (
        _SQLITE_REPORT_PYTHON
        + "\nreport=report_database(sys.argv[1],json.loads(sys.argv[2]))"
        + "\nprint(json.dumps(report,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False))\n"
    )
    try:
        output = runtime_update._checked_stdout(
            [
                *docker_prefix,
                "exec",
                container_id,
                "/opt/venv/bin/python",
                "-c",
                script,
                database_path,
                comparison_json,
            ],
            label="Navigator Persistence live SQLite inspection",
            timeout_seconds=30,
        )
        report = runtime_update._decode_json_output(output, "live SQLite report")
    except (runtime_update.RuntimeUpdateError, subprocess.SubprocessError) as error:
        raise StageTransitionError(
            "live Navigator Persistence database could not be inspected"
        ) from error
    _validate_sqlite_report_shape(report, "live Navigator Persistence database report")
    report["objects"] = sorted(report["objects"])
    return report


def _sqlite_comparison_contract(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Build a closed old-column reconciliation contract from one validated report."""

    _validate_sqlite_report_shape(snapshot, "pre-update Persistence snapshot")
    return {
        table: {
            "columns": [column["name"] for column in record["columns"]],
            "primaryKeyColumns": record["primaryKeyColumns"],
        }
        for table, record in snapshot["tables"].items()
    }


def _active_persistence_writer_lease_count(
    *, docker_prefix: list[str], container_id: str, database_path: str
) -> int:
    """Count unexpired Persistence write leases without reading their identifiers."""

    if FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError("active Persistence container identity is malformed")
    script = (
        "import json,sqlite3,sys,time\n"
        "path=sys.argv[1]\n"
        "db=sqlite3.connect('file:'+path+'?mode=ro',uri=True)\n"
        "columns={row[1] for row in db.execute('PRAGMA table_info(sessions)')}\n"
        "required={'writer_token_hash','lease_expires_at'}\n"
        "if not required <= columns: raise RuntimeError('writer lease columns unavailable')\n"
        "now=int(time.time()*1000)\n"
        "count=db.execute('SELECT COUNT(*) FROM sessions WHERE writer_token_hash IS NOT NULL AND lease_expires_at>?',(now,)).fetchone()[0]\n"
        "db.close()\n"
        "print(json.dumps({'activeWriterLeaseCount':int(count)},separators=(',',':')))\n"
    )
    try:
        output = runtime_update._checked_stdout(
            [
                *docker_prefix,
                "exec",
                container_id,
                "/opt/venv/bin/python",
                "-c",
                script,
                database_path,
            ],
            label="Navigator Persistence active writer lease readback",
            timeout_seconds=15,
        )
        report = runtime_update._decode_json_output(output, "active Persistence writer leases")
    except (runtime_update.RuntimeUpdateError, subprocess.SubprocessError) as error:
        raise StageTransitionError(
            "active Persistence writer leases could not be verified"
        ) from error
    count = report.get("activeWriterLeaseCount") if isinstance(report, dict) else None
    if (
        not isinstance(report, dict)
        or set(report) != {"activeWriterLeaseCount"}
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
    ):
        raise StageTransitionError("active Persistence writer lease report is malformed")
    return count


def _verify_persistence_update_quiescence(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> None:
    """Prove the drained database is unchanged and has no live writer before replacement."""

    baseline = _validate_sqlite_report_shape(
        journal.get("database", {}).get("snapshot"),
        "pre-update Persistence snapshot",
    )
    persistence = plan.get("activeServices", {}).get(PERSISTENCE_IMAGE_UPDATE_TARGET)
    if not isinstance(persistence, dict):
        raise StageTransitionError("active Persistence service identity is unavailable")
    current = _live_sqlite_report(
        docker_prefix=docker_prefix,
        container_id=persistence["containerId"],
        database_path=plan["database"]["containerPath"],
        comparison_contract=_sqlite_comparison_contract(baseline),
    )
    _validate_persistence_same_schema_reports(baseline, current)
    active_writer_leases = _active_persistence_writer_lease_count(
        docker_prefix=docker_prefix,
        container_id=persistence["containerId"],
        database_path=plan["database"]["containerPath"],
    )
    if active_writer_leases != 0:
        raise StageTransitionError(
            "Persistence image update requires zero unexpired Session writer leases"
        )
    journal["database"]["preUpdateQuiescence"] = {
        "verified": True,
        "activeWriterLeaseCount": active_writer_leases,
        "schemaSha256": baseline["schemaSha256"],
        "tableCount": len(baseline["tables"]),
        "rowsDigestSha256": _sha256_bytes(
            _canonical_json(
                {
                    table: {
                        "rowCount": record["rowCount"],
                        "rowsDigest": record["rowsDigest"],
                    }
                    for table, record in sorted(baseline["tables"].items())
                }
            )
        ),
        "checkedAtUtc": _utc_now(),
    }
    _write_journal(
        journal_path,
        journal,
        "PERSISTENCE_SAME_SCHEMA_QUIESCENCE_VERIFIED",
    )


def _validate_persistence_same_schema_update(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Verify that a replaced Persistence image preserved every schema object and row."""

    persistence = journal.get("targetProgress", {}).get(PERSISTENCE_IMAGE_UPDATE_TARGET, {})
    container_id = (
        persistence.get("candidateContainerId") if isinstance(persistence, dict) else None
    )
    if not isinstance(container_id, str) or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError("candidate Persistence container identity is unavailable")
    before = _validate_sqlite_report_shape(
        journal.get("database", {}).get("snapshot"),
        "pre-update Persistence snapshot",
    )
    current = _live_sqlite_report(
        docker_prefix=docker_prefix,
        container_id=container_id,
        database_path=plan["database"]["containerPath"],
        comparison_contract=_sqlite_comparison_contract(before),
    )
    _validate_persistence_same_schema_reports(before, current)
    journal["database"]["postMigration"] = current
    journal["database"]["restorePerformed"] = False
    _write_journal(journal_path, journal, "PERSISTENCE_SAME_SCHEMA_READBACK_VERIFIED")
    return current


def _validate_database_migration(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Verify additive schema changes and exact preservation of every pre-existing row."""

    persistence = journal.get("targetProgress", {}).get("navigator-persistence", {})
    container_id = (
        persistence.get("candidateContainerId") if isinstance(persistence, dict) else None
    )
    if not isinstance(container_id, str) or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError("candidate Persistence container identity is unavailable")
    before = journal.get("database", {}).get("snapshot")
    _validate_sqlite_report_shape(before, "pre-transition SQLite snapshot")
    comparison_contract = {
        table: {
            "columns": [column["name"] for column in record["columns"]],
            "primaryKeyColumns": record["primaryKeyColumns"],
        }
        for table, record in before["tables"].items()
    }
    current = _live_sqlite_report(
        docker_prefix=docker_prefix,
        container_id=container_id,
        database_path=plan["database"]["containerPath"],
        comparison_contract=comparison_contract,
    )
    _validate_database_migration_reports(before, current)
    journal["database"]["postMigration"] = current
    journal["database"]["restorePerformed"] = False
    _write_journal(journal_path, journal, "DATABASE_MIGRATION_VERIFIED")
    return current


def _validate_work_credential_health_table(
    record: dict[str, Any], *, require_empty: bool = True
) -> None:
    """Pin the one additive Navigator 17700 credential-health table to its source schema."""

    expected = [
        ("organization_id", "TEXT", True, None, 1),
        ("workspace_id", "TEXT", True, None, 2),
        ("connection_id", "TEXT", True, None, 3),
        ("provider", "TEXT", True, None, 0),
        ("generation", "INTEGER", True, None, 0),
        ("fingerprint", "TEXT", True, None, 0),
        ("decision", "TEXT", True, None, 0),
        ("checked_at", "TEXT", False, None, 0),
        ("expires_at", "TEXT", False, None, 0),
        ("reason_code", "TEXT", False, None, 0),
        ("incident_id", "TEXT", False, None, 0),
        ("incident_started_at", "INTEGER", False, None, 0),
        ("last_notified_at", "INTEGER", False, None, 0),
        ("reminder_count", "INTEGER", True, "0", 0),
        ("updated_at", "INTEGER", True, None, 0),
    ]
    observed = [
        (
            row.get("name"),
            row.get("type"),
            row.get("notNull"),
            row.get("default"),
            row.get("primaryKeyOrdinal"),
        )
        for row in record.get("columns", [])
    ]
    if (
        observed != expected
        or _normalize_sqlite_ddl(record.get("createSql"))
        != _normalize_sqlite_ddl(SQLITE_CREDENTIAL_HEALTH_TABLE_SQL)
        or record.get("primaryKeyColumns") != ["organization_id", "workspace_id", "connection_id"]
        or record.get("foreignKeys") != []
        or record.get("checkConstraints") != []
        or record.get("uniqueIndexes") != []
        or (require_empty and record.get("rowCount") != 0)
        or record.get("emptyOrganizationIdRows") != 0
    ):
        raise StageTransitionError(
            "additive credential-health table differs from the pinned Navigator 17700 schema"
        )


def _validate_preexisting_credential_health_schema(report: Any) -> dict[str, Any]:
    """Require the exact previously migrated health table before continuing provider bootstrap."""

    snapshot = _validate_sqlite_report_shape(
        report, "pre-existing Persistence candidate schema readback"
    )
    table = snapshot["tables"].get("work_credential_authorization_health")
    if not isinstance(table, dict):
        raise StageTransitionError(
            "Persistence already uses the candidate image but lacks its credential-health table"
        )
    _validate_work_credential_health_table(table, require_empty=False)
    return snapshot


def _validate_bootstrap_persistence_baseline(
    plan: dict[str, Any], report: Any
) -> dict[str, Any] | None:
    """Require the credential-health schema when bootstrap reuses the candidate Persistence image."""

    persistence = plan["candidateServices"]["navigator-persistence"]
    if plan["phase"] == "provider-bootstrap" and persistence["active"] == persistence["candidate"]:
        return _validate_preexisting_credential_health_schema(report)
    return None


def _normalize_sqlite_ddl(value: Any) -> str:
    """Normalize whitespace and SQLite's removed IF NOT EXISTS clause for pinned DDL."""

    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"\bIF\s+NOT\s+EXISTS\b", "", value, flags=re.IGNORECASE)
    return " ".join(normalized.split()).strip().rstrip(";").lower()


def _validate_database_migration_reports(before: dict[str, Any], current: dict[str, Any]) -> None:
    """Permit one empty declared table while requiring unchanged old DDL and row streams."""

    _validate_sqlite_report_shape(before, "pre-transition SQLite snapshot")
    _validate_sqlite_report_shape(current, "post-transition SQLite report")
    if current["userVersion"] != before["userVersion"]:
        raise StageTransitionError(
            "Persistence changed user_version outside the declared additive migration"
        )

    old_objects = {f"{row['type']}:{row['name']}": row for row in before["schemaObjects"]}
    new_objects = {f"{row['type']}:{row['name']}": row for row in current["schemaObjects"]}
    removed = set(old_objects) - set(new_objects)
    added = set(new_objects) - set(old_objects)
    if removed:
        raise StageTransitionError("Persistence removed an existing SQLite schema object")
    if added - SQLITE_ADDITIVE_SCHEMA_OBJECTS:
        raise StageTransitionError("Persistence added an undeclared SQLite schema object")
    for object_name in old_objects:
        if old_objects[object_name]["sql"] != new_objects[object_name]["sql"]:
            raise StageTransitionError(
                f"Persistence changed the DDL for an existing SQLite object: {object_name}"
            )

    for table, old_record in before["tables"].items():
        new_record = current["tables"].get(table)
        if not isinstance(new_record, dict):
            raise StageTransitionError(f"Persistence removed existing table {table}")
        for field in (
            "columns",
            "primaryKeyColumns",
            "foreignKeys",
            "checkConstraints",
            "uniqueIndexes",
        ):
            if new_record[field] != old_record[field]:
                raise StageTransitionError(
                    f"Persistence changed existing table {table} {field} metadata"
                )
        if (
            new_record["rowCount"] != old_record["rowCount"]
            or new_record.get("preservedRowsCount") != old_record["rowCount"]
            or new_record.get("preservedRowsDigest") != old_record["rowsDigest"]
            or new_record["rowsDigest"] != old_record["rowsDigest"]
        ):
            raise StageTransitionError(
                f"Persistence changed or lost pre-existing rows in table {table}"
            )

    for object_name in added:
        object_record = new_objects[object_name]
        if object_name == "table:work_credential_authorization_health":
            _validate_work_credential_health_table(
                current["tables"]["work_credential_authorization_health"]
            )
        elif object_record["type"] == "index" and object_name in SQLITE_ADDITIVE_SCHEMA_OBJECTS:
            if not isinstance(object_record["sql"], str) or not object_record[
                "sql"
            ].lstrip().lower().startswith("create index "):
                raise StageTransitionError(
                    "only declared non-unique additive indexes are supported"
                )
        else:
            raise StageTransitionError("declared SQLite schema addition has an unsupported type")
    current["migrationCompatibility"] = {
        "class": "ADDITIVE_ONLY_EXISTING_SCHEMA_AND_ROWS_PRESERVED",
        "userVersionUnchanged": True,
        "preservedExistingTables": sorted(before["tables"]),
        "addedSchemaObjects": sorted(added),
    }


def _validate_persistence_same_schema_reports(
    before: dict[str, Any], current: dict[str, Any]
) -> None:
    """Require byte-stable schema metadata and exact logical rows for image-only updates.

    中文：Persistence 镜像更新只允许同一 schema 和全部逻辑行保持一致。
    """

    _validate_sqlite_report_shape(before, "pre-update Persistence snapshot")
    _validate_sqlite_report_shape(current, "post-update Persistence report")
    for field in ("userVersion", "objects", "schemaObjects", "schemaSha256"):
        if current[field] != before[field]:
            raise StageTransitionError(f"Persistence same-schema update changed SQLite {field}")
    if set(current["tables"]) != set(before["tables"]):
        raise StageTransitionError("Persistence same-schema update changed the table inventory")

    exact_table_fields = (
        "columns",
        "createSql",
        "primaryKeyColumns",
        "foreignKeys",
        "checkConstraints",
        "uniqueIndexes",
        "rowCount",
        "rowsDigest",
        "emptyOrganizationIdRows",
    )
    for table, old_record in before["tables"].items():
        new_record = current["tables"][table]
        for field in exact_table_fields:
            if old_record.get(field) != new_record.get(field):
                raise StageTransitionError(
                    f"Persistence same-schema update changed {table} {field}"
                )
    current["migrationCompatibility"] = {
        "class": "SAME_SCHEMA_AND_ALL_ROWS_PRESERVED",
        "schemaSha256": before["schemaSha256"],
        "preservedTables": sorted(before["tables"]),
    }


def _validate_post_update_capacity(
    *,
    compose: dict[str, Any],
    services: dict[str, dict[str, Any]],
    docker_prefix: list[str],
    minimum_available_mib: int,
) -> dict[str, Any]:
    """Match RSS by sealed container ID and enforce the fixed host reserve before release."""

    available_mib = preflight._parse_memavailable()
    total_memory_mib = preflight._parse_meminfo()
    if available_mib < minimum_available_mib:
        raise StageTransitionError(
            "post-transition MemAvailable fell below the configured host reserve"
        )
    service_by_container_id: dict[str, str] = {}
    for service_name, service in services.items():
        container_id = service.get("containerId") if isinstance(service, dict) else None
        if (
            not isinstance(container_id, str)
            or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None
        ):
            raise StageTransitionError("post-transition service container ID is malformed")
        if container_id in service_by_container_id:
            raise StageTransitionError("post-transition services share a container ID")
        service_by_container_id[container_id] = service_name
    if not service_by_container_id:
        raise StageTransitionError("post-transition RSS inventory has no services")

    # Match stats to the sealed inventory by exact ID; service-name prefixes overlap.
    # 精确容器 ID 可避免 navigator 与 navigator-persistence 等名称前缀重叠。
    stats = runtime_update._checked_stdout(
        [
            *docker_prefix,
            "stats",
            "--no-stream",
            "--no-trunc",
            "--format",
            "{{.ID}}|{{.MemUsage}}",
            *sorted(service_by_container_id),
        ],
        label="post-transition RSS inventory",
        timeout_seconds=30,
    )
    observed: dict[str, float] = {}
    seen_container_ids: set[str] = set()
    for line in stats.splitlines():
        container_id, separator, usage = line.partition("|")
        if not separator or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
            raise StageTransitionError("Docker returned a malformed RSS container ID")
        if container_id in seen_container_ids:
            raise StageTransitionError("Docker returned a duplicate RSS container ID")
        seen_container_ids.add(container_id)
        service_name = service_by_container_id.get(container_id)
        if service_name is None:
            raise StageTransitionError("Docker returned RSS for an unknown service container")
        observed[service_name] = preflight._docker_memory_usage_mib(usage)
    if set(observed) != set(services):
        raise StageTransitionError(
            "Docker did not return complete per-service RSS after transition"
        )
    total_rss_mib = sum(observed.values())
    if total_rss_mib + minimum_available_mib > total_memory_mib:
        raise StageTransitionError("post-transition RSS plus host reserve exceeds guest memory")
    return {
        "memAvailableMiB": available_mib,
        "hostMemoryMiB": total_memory_mib,
        "minimumMemAvailableMiB": minimum_available_mib,
        "totalRssMiB": round(total_rss_mib, 2),
        "rssByServiceMiB": {name: round(value, 2) for name, value in sorted(observed.items())},
    }


def _selection_summary(document: dict[str, Any]) -> dict[str, Any]:
    """Return the safe runtime selection facts with no environment values."""

    return {
        "schemaVersion": document.get("schemaVersion"),
        "project": document.get("project"),
        "operationId": document.get("operationId"),
        "planSha256": document.get("planSha256"),
        "updatedAtUtc": document.get("updatedAtUtc"),
    }


def create_plan(
    *,
    phase: str,
    image_update_target: str | None = None,
    memory_update_target: str | None = None,
    listener_repair: bool = False,
    active_compose_files: list[Path],
    active_env_files: list[Path],
    active_image_lock: Path,
    candidate_compose_files: list[Path],
    candidate_env_files: list[Path],
    candidate_image_lock: Path,
    secrets_dir: Path,
    active_config_dir: Path,
    candidate_config_dir: Path,
    candidate_huggingface_seed: Path,
    candidate_runpod_key_file: Path | None,
    connector_auth_evidence: Path,
    connector_auth_task_id: str,
    notification_anchor_file: Path | None,
    notification_provenance_file: Path | None,
    active_selection_file: Path | None,
    promote_selection_file: Path,
    backup_dir: Path,
    journal_dir: Path,
    plan_path: Path,
    docker_prefix: list[str],
    drain_timeout_seconds: int,
    health_timeout_seconds: int,
    max_memory_mib: int,
    max_cpus: float,
    required_free_gib: float,
    minimum_mem_available_mib: int,
    account_center_unknown_preservation_evidence: Path | None = None,
    ingress_exposure: str = "public-cutover",
    host_caddy_binary: str = "/usr/local/bin/caddy",
    openssl_binary: str = "openssl",
) -> dict[str, Any]:
    """Build a private, read-only plan bound to current containers and exact candidate files."""

    runtime_update._require_update_operator()
    if phase not in PHASES:
        raise StageTransitionError(
            "phase must be provider-bootstrap or issued-connector-activation; Microsoft Graph provider bootstrap is also supported"
        )
    if image_update_target is not None and (
        not isinstance(image_update_target, str) or image_update_target not in IMAGE_UPDATE_TARGETS
    ):
        raise StageTransitionError(
            "image update target must be Account Center, Cloud Management, Exchange, GitHub Issues, Hugging Face, Navigator, or Persistence"
        )
    if image_update_target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET and (
        phase not in ACCOUNT_CENTER_IMAGE_UPDATE_PHASES
        or max_memory_mib != 3584
        or max_cpus != 1.95
    ):
        raise StageTransitionError(
            "Account Center image updates require the selected Graph phase and 3584 MiB/1.95 CPU budget"
        )
    if (
        image_update_target == EXCHANGE_IMAGE_UPDATE_TARGET
        and phase == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
        and (max_memory_mib != 3584 or max_cpus != 1.95)
    ):
        raise StageTransitionError(
            "Exchange Graph-bootstrap image updates require the 3584 MiB/1.95 CPU budget"
        )
    if account_center_unknown_preservation_evidence is not None and (
        image_update_target != ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
    ):
        raise StageTransitionError(
            "historical UNKNOWN preservation evidence is only valid for an Account Center image update"
        )
    if image_update_target is not None and memory_update_target is not None:
        raise StageTransitionError("image and memory update modes are mutually exclusive")
    if memory_update_target is not None and (
        not isinstance(memory_update_target, str)
        or memory_update_target not in MEMORY_UPDATE_TARGETS
    ):
        raise StageTransitionError("memory update target must be one existing runtime service")
    if memory_update_target is not None and phase != "issued-connector-activation":
        raise StageTransitionError("memory updates require issued-connector-activation")
    if (
        image_update_target == HUGGINGFACE_IMAGE_UPDATE_TARGET
        and phase != "issued-connector-activation"
    ):
        raise StageTransitionError(
            "Hugging Face image updates require issued-connector-activation and the existing connector runtime"
        )
    if (
        image_update_target == GITHUB_ISSUES_IMAGE_UPDATE_TARGET
        and phase != "issued-connector-activation"
    ):
        raise StageTransitionError(
            "GitHub Issues image updates require issued-connector-activation and the existing connector runtime"
        )
    if (
        image_update_target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
        and phase not in ACCOUNT_CENTER_IMAGE_UPDATE_PHASES
    ):
        raise StageTransitionError(
            "Account Center image updates require the selected Graph bootstrap or issued activation phase"
        )
    if image_update_target == CLOUD_IMAGE_UPDATE_TARGET and phase not in PHASES:
        raise StageTransitionError(
            "Cloud Management image updates require provider-bootstrap or issued-connector-activation"
        )
    if (
        image_update_target == PERSISTENCE_IMAGE_UPDATE_TARGET
        and phase != "issued-connector-activation"
    ):
        raise StageTransitionError(
            "Persistence image updates require issued-connector-activation and the existing connector runtime"
        )
    if listener_repair and (
        phase != "provider-bootstrap"
        or image_update_target is not None
        or memory_update_target is not None
    ):
        raise StageTransitionError(
            "provider listener repair is a separate provider-bootstrap operation"
        )
    if ingress_exposure not in {"public-cutover", "host-fronted-staging"}:
        raise StageTransitionError(
            "ingress exposure must be public-cutover or host-fronted-staging"
        )
    if not isinstance(host_caddy_binary, str) or not host_caddy_binary:
        raise StageTransitionError("host Caddy executable path is malformed")
    if not isinstance(openssl_binary, str) or not openssl_binary:
        raise StageTransitionError("OpenSSL executable path is malformed")
    runtime_update._validate_docker_prefix(docker_prefix)
    if not 60 <= drain_timeout_seconds <= 900 or not 30 <= health_timeout_seconds <= 600:
        raise StageTransitionError("drain/health timeouts are outside their bounded ranges")
    if (
        isinstance(max_memory_mib, bool)
        or not isinstance(max_memory_mib, int)
        or max_memory_mib <= 0
        or not isinstance(max_cpus, (int, float))
        or isinstance(max_cpus, bool)
        or max_cpus <= 0
        or not isinstance(required_free_gib, (int, float))
        or isinstance(required_free_gib, bool)
        or required_free_gib < 0
        or isinstance(minimum_mem_available_mib, bool)
        or not isinstance(minimum_mem_available_mib, int)
        or minimum_mem_available_mib <= 0
    ):
        raise StageTransitionError("host resource limits are malformed")
    if memory_update_target is not None and (
        max_memory_mib > 3584
        or max_cpus > 1.95
        or required_free_gib < 2.0
        or minimum_mem_available_mib < MEMORY_RESERVE_DEFAULT_MIB
    ):
        raise StageTransitionError(
            "memory updates require the canonical 3584 MiB/1.95 CPU/2 GiB/1 GiB host safeguards"
        )
    _validate_output_directory(plan_path)
    active_config_dir = _validate_private_directory(
        active_config_dir, "active config", expected_uid=10001
    )
    candidate_config_dir = _validate_private_directory(
        candidate_config_dir, "candidate config", expected_uid=10001
    )
    secrets_dir = _validate_private_directory(secrets_dir, "active secrets", expected_uid=10001)
    backup_dir = _validate_private_directory(backup_dir, "SQLite backup", require_artifact=True)
    _validate_config_directory_policy(
        image_update_target=image_update_target,
        memory_update_target=memory_update_target,
        listener_repair=listener_repair,
        active_config_dir=active_config_dir,
        candidate_config_dir=candidate_config_dir,
    )
    if not connector_auth_task_id or len(connector_auth_task_id) > 128:
        raise StageTransitionError("connector auth task ID is malformed")
    if phase == "issued-connector-activation" and (
        notification_anchor_file is None or notification_provenance_file is None
    ):
        raise StageTransitionError("notification anchor/provenance are required for activation")
    if phase in {"provider-bootstrap", OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE} and (
        notification_anchor_file is not None or notification_provenance_file is not None
    ):
        raise StageTransitionError("provider bootstrap must keep credential notifications disabled")

    active_inputs = {
        "composeFiles": _file_records(active_compose_files, "active Compose file"),
        "envFiles": _file_records(active_env_files, "active environment file"),
        "imageLock": _file_record(active_image_lock, "active image lock"),
        "configDirectoryFiles": _directory_manifest(active_config_dir, "active config"),
    }
    candidate_inputs = {
        "composeFiles": _file_records(candidate_compose_files, "candidate Compose file"),
        "envFiles": _file_records(candidate_env_files, "candidate environment file"),
        "imageLock": _file_record(candidate_image_lock, "candidate image lock"),
        "configDirectoryFiles": _directory_manifest(candidate_config_dir, "candidate config"),
        "huggingfaceSeed": _file_record(candidate_huggingface_seed, "candidate HF OAuth seed"),
        "runpodKey": (
            _file_record(candidate_runpod_key_file, "candidate RunPod key")
            if candidate_runpod_key_file is not None
            else None
        ),
        "connectorAuthEvidence": _file_record(connector_auth_evidence, "connector auth evidence"),
        "notificationAnchor": (
            _file_record(notification_anchor_file, "notification source anchor")
            if notification_anchor_file is not None
            else None
        ),
        "notificationProvenance": (
            _file_record(notification_provenance_file, "notification provenance")
            if notification_provenance_file is not None
            else None
        ),
    }
    if account_center_unknown_preservation_evidence is not None:
        candidate_inputs["accountCenterUnknownPreservationEvidence"] = (
            _operator_private_file_record(
                account_center_unknown_preservation_evidence.resolve(strict=True),
                "Account Center unknown preservation evidence",
            )
        )
    active_lock = runtime_update._load_lock(active_image_lock)
    candidate_lock = runtime_update._load_lock(candidate_image_lock)
    try:
        active_compose = preflight._compose_config(
            docker_prefix, active_compose_files, active_env_files
        )
        candidate_compose = preflight._compose_config(
            docker_prefix, candidate_compose_files, candidate_env_files
        )
    except (OSError, preflight.PreflightError, subprocess.SubprocessError) as error:
        raise StageTransitionError("active or candidate Compose could not be rendered") from error
    active_inputs["secretFiles"] = _secret_manifest(active_compose, "active")
    candidate_inputs["secretFiles"] = _secret_manifest(candidate_compose, "candidate")
    if (
        image_update_target is None
        and memory_update_target is None
        and not listener_repair
        and phase != OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
    ):
        _validate_active_candidate_configs(
            active_config_dir,
            candidate_config_dir,
            active_compose,
            candidate_compose,
            phase=phase,
        )
    _validate_active_candidate_secrets(active_compose, candidate_compose)
    _validate_candidate_secret_paths(
        candidate=candidate_compose,
        active_secrets_dir=secrets_dir,
        huggingface_seed_file=candidate_huggingface_seed,
        runpod_key_file=candidate_runpod_key_file,
        phase=phase,
    )
    targets, target_images = _validate_phase_topology(
        phase=phase,
        image_update_target=image_update_target,
        memory_update_target=memory_update_target,
        listener_repair=listener_repair,
        active=active_compose,
        candidate=candidate_compose,
        active_lock=active_lock,
        candidate_lock=candidate_lock,
    )
    if active_compose.get("name") != candidate_compose.get("name"):
        raise StageTransitionError("active and candidate Compose project names differ")
    project = active_compose["name"]
    try:
        candidate_preflight = preflight.run_preflight(
            mode="connector-auth-readonly",
            compose_files=[path.resolve() for path in candidate_compose_files],
            env_files=[path.resolve() for path in candidate_env_files],
            image_lock=candidate_image_lock.resolve(),
            secrets_dir=secrets_dir,
            config_dir=candidate_config_dir,
            github_hf_evidence=None,
            azure_gcp_ledger=None,
            runpod_ledger=None,
            azure_gcp_api_evidence=None,
            runpod_api_evidence=None,
            docker_prefix=docker_prefix,
            max_memory_mib=max_memory_mib,
            max_cpus=max_cpus,
            required_free_gib=required_free_gib,
            minimum_mem_available_mib=minimum_mem_available_mib,
            connector_auth_phase=phase,
            connector_auth_evidence=connector_auth_evidence.resolve(strict=True),
            connector_auth_task_id=connector_auth_task_id,
            huggingface_seed_file=candidate_huggingface_seed.resolve(strict=True),
            candidate_runpod_key_file=(
                candidate_runpod_key_file.resolve(strict=True)
                if candidate_runpod_key_file is not None
                else None
            ),
            notification_anchor_file=(
                notification_anchor_file.resolve(strict=True)
                if notification_anchor_file is not None
                else None
            ),
            notification_provenance_file=(
                notification_provenance_file.resolve(strict=True)
                if notification_provenance_file is not None
                else None
            ),
            ingress_exposure=ingress_exposure,
            host_caddy_binary=host_caddy_binary,
            openssl_binary=openssl_binary,
        )
    except (OSError, preflight.PreflightError, BuildError, subprocess.SubprocessError) as error:
        raise StageTransitionError(
            "credential read-only candidate preflight blocked the transition"
        ) from error
    if (
        candidate_preflight.get("mode") != "connector-auth-readonly"
        or candidate_preflight.get("credentialReadOnlyRuntimeReady") is not True
        or candidate_preflight.get("fullCloudWriteDeploymentReady") is not False
        or candidate_preflight.get("lifecycleAcceptance") != "NOT_RUN"
    ):
        raise StageTransitionError(
            "candidate preflight did not return the scoped read-only readiness contract"
        )
    if image_update_target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET and (
        candidate_preflight.get("composeCgroupMemoryLimitMiB") != 3584
        or candidate_preflight.get("composeCgroupCpuLimit") != 1.95
    ):
        raise StageTransitionError(
            "Account Center candidate Compose must retain the 3584 MiB/1.95 CPU budget"
        )
    if image_update_target == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET and (
        candidate_preflight.get("composeCgroupMemoryLimitMiB") != 3584
        or candidate_preflight.get("composeCgroupCpuLimit") != 1.95
    ):
        raise StageTransitionError(
            "Workspace Authority candidate Compose must retain the 3584 MiB/1.95 CPU budget"
        )
    if (
        image_update_target == EXCHANGE_IMAGE_UPDATE_TARGET
        and phase == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
        and (
            candidate_preflight.get("composeCgroupMemoryLimitMiB") != 3584
            or candidate_preflight.get("composeCgroupCpuLimit") != 1.95
        )
    ):
        raise StageTransitionError(
            "Exchange Graph-bootstrap candidate Compose must retain the 3584 MiB/1.95 CPU budget"
        )
    if (
        image_update_target is not None
        or memory_update_target is not None
        or listener_repair
        or phase == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
    ) and active_selection_file is None:
        raise StageTransitionError(
            "image-only/repair update requires the existing runtime selector v2"
        )
    if (
        active_selection_file is not None
        and active_selection_file.resolve() != promote_selection_file.resolve()
    ):
        raise StageTransitionError(
            "active and promoted runtime selection must use the same stable pointer path"
        )
    selection_state = _selection_file_state(
        promote_selection_file,
        active_inputs,
        project=project,
        config_dir=active_config_dir,
        secrets_dir=secrets_dir,
        active_service_names=set(_runtime_service_inventory(active_compose)),
    )
    active_mount_identity = (
        selection_state["document"]["runtimeConfigIdentity"]
        if selection_state["document"] is not None
        else _runtime_config_identity_for_compose(active_compose)
    )
    active_mount_identity = _validate_runtime_config_identity(
        active_mount_identity,
        expected_services=set(_runtime_service_inventory(active_compose)),
        label="active runtime configuration identity",
    )
    candidate_mount_identity = _candidate_runtime_config_identity(
        active_identity=active_mount_identity,
        candidate_compose=candidate_compose,
        target_services=set(targets),
    )
    _verify_runtime_config_identity_sources(active_mount_identity, "active runtime configuration")
    _verify_runtime_config_identity_sources(
        candidate_mount_identity, "candidate runtime configuration"
    )
    if listener_repair:
        _validate_listener_repair_selection_contract(
            phase=phase,
            project=project,
            selection_record=selection_state,
            selection_path=str(promote_selection_file.resolve()),
            active_config_dir=str(active_config_dir),
            candidate_config_dir=str(candidate_config_dir),
            active_secrets_dir=str(secrets_dir),
            active_inputs=active_inputs,
            candidate_inputs=candidate_inputs,
            runtime_config_identity={
                "active": active_mount_identity,
                "candidate": candidate_mount_identity,
            },
        )
    else:
        _validate_image_update_selection_contract(
            target=image_update_target,
            memory_update_target=memory_update_target,
            phase=phase,
            project=project,
            selection_record=selection_state,
            selection_path=str(promote_selection_file.resolve()),
            active_config_dir=str(active_config_dir),
            candidate_config_dir=str(candidate_config_dir),
            active_secrets_dir=str(secrets_dir),
            active_inputs=active_inputs,
            candidate_inputs=candidate_inputs,
            runtime_config_identity={
                "active": active_mount_identity,
                "candidate": candidate_mount_identity,
            },
        )
    edge_service = _runtime_service_inventory(active_compose).get("teams-ingress")
    if not isinstance(edge_service, dict):
        raise StageTransitionError("active Teams ingress service is missing")
    active_services = _live_service_inventory(
        compose=active_compose,
        image_lock=active_lock,
        docker_prefix=docker_prefix,
        mount_identity_by_service=active_mount_identity,
    )
    edge_container = active_services.get("teams-ingress")
    navigator_container = active_services.get("navigator")
    if not isinstance(edge_container, dict) or not isinstance(navigator_container, dict):
        raise StageTransitionError("active Teams ingress or Navigator container is missing")
    cloud_runner_state = None
    listener_repair_state = None
    if image_update_target == CLOUD_IMAGE_UPDATE_TARGET or listener_repair:
        exchange_service = active_services.get("exchange")
        if not isinstance(exchange_service, dict):
            raise StageTransitionError("active Exchange service is missing for provider transition")
        if phase == "provider-bootstrap" or listener_repair:
            cloud_runner_state = _cloud_runner_state_record(
                active_compose=active_compose,
                candidate_compose=candidate_compose,
                exchange_service=exchange_service,
                docker_prefix=docker_prefix,
            )
    if listener_repair:
        listener_repair_state = _listener_repair_state_record(
            active_compose=active_compose,
            candidate_compose=candidate_compose,
            active_services=active_services,
            docker_prefix=docker_prefix,
        )
        issued_state = _read_live_exchange_issued_state(
            compose=candidate_compose,
            container_id=active_services["exchange"]["containerId"],
            image_id=active_services["exchange"]["imageId"],
            docker_prefix=docker_prefix,
        )
        listener_repair_state["issuedState"] = {
            "exchangeContainerId": issued_state["exchangeContainerId"],
            "exchangeImageId": issued_state["exchangeImageId"],
            "issuedStateSha256": issued_state["issuedStateSha256"],
            "catalogStatus": issued_state["catalogStatus"],
            "profileCount": issued_state["profileCount"],
            "connectionCount": issued_state["connectionCount"],
            "bindingCount": issued_state["bindingCount"],
            "verifiedProviderIds": issued_state["verifiedProviderIds"],
        }
    edge_status = runtime_update._current_edge_status(
        docker_prefix, edge_container["containerId"], timeout_seconds=15
    )
    if edge_status["paused"] is not False or edge_status["dead_count"] != 0:
        raise StageTransitionError(
            "Teams ingress must be accepting with no dead-letter events before planning"
        )
    navigator_status = runtime_update._current_navigator_status(
        docker_prefix, navigator_container["containerId"], timeout_seconds=15
    )
    if navigator_status["mode"] != "accepting":
        raise StageTransitionError("Navigator must be accepting before transition planning")

    account_center_image_update_state = None
    if image_update_target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET:
        account_center = active_services.get(ACCOUNT_CENTER_IMAGE_UPDATE_TARGET)
        if not isinstance(account_center, dict):
            raise StageTransitionError("active Account Center service is missing")
        account_center_image_update_state = {
            "database": _account_center_database_state(
                compose=active_compose,
                project=project,
                container_id=account_center["containerId"],
                image_ref=account_center["imageRef"],
                image_id=account_center["imageId"],
                docker_prefix=docker_prefix,
                include_startup_recovery_counts=True,
            ),
            "runtimeCapacity": _memory_update_actual_capacity(
                compose=active_compose,
                services=active_services,
                max_memory_mib=3584,
                max_cpus=1.95,
                docker_prefix=docker_prefix,
            ),
        }
        preservation_evidence_record = candidate_inputs.get(
            "accountCenterUnknownPreservationEvidence"
        )
        if preservation_evidence_record is not None:
            selected_image = target_images.get(ACCOUNT_CENTER_IMAGE_UPDATE_TARGET)
            candidate_image = (
                selected_image.get("candidate") if isinstance(selected_image, dict) else None
            )
            if not isinstance(candidate_image, dict):
                raise StageTransitionError(
                    "Account Center candidate image is unavailable for UNKNOWN preservation"
                )
            account_center_image_update_state["preservedUnknown"] = (
                _account_center_preserved_unknown_summary(
                    evidence_path=Path(preservation_evidence_record["path"]),
                    evidence_record=preservation_evidence_record,
                    database_state=account_center_image_update_state["database"],
                    candidate_compose=candidate_compose,
                    candidate_image_id=candidate_image.get("imageId"),
                    candidate_image_lock_sha256=candidate_inputs["imageLock"]["sha256"],
                )
            )
        _validate_account_center_image_update_state(account_center_image_update_state)

    operation_id = str(uuid.uuid4())
    backup_path = backup_dir / f"{operation_id}.navigator-sessions.sqlite3"
    if memory_update_target is None and (backup_path.exists() or backup_path.is_symlink()):
        raise StageTransitionError("online SQLite backup output already exists")
    selection_backup_path = backup_dir / f"{operation_id}.active-selection.previous.json"
    if selection_backup_path.exists() or selection_backup_path.is_symlink():
        raise StageTransitionError("active selection backup output already exists")
    plan: dict[str, Any] = {
        "schemaVersion": PLAN_SCHEMA,
        "operationId": operation_id,
        "phase": phase,
        **({"imageUpdateTarget": image_update_target} if image_update_target is not None else {}),
        **(
            {"memoryUpdateTarget": memory_update_target} if memory_update_target is not None else {}
        ),
        **({"cloudRunnerState": cloud_runner_state} if cloud_runner_state is not None else {}),
        **(
            {"listenerRepairState": listener_repair_state}
            if listener_repair_state is not None
            else {}
        ),
        **(
            {"accountCenterImageUpdateState": account_center_image_update_state}
            if account_center_image_update_state is not None
            else {}
        ),
        "createdAtUtc": _utc_now(),
        "project": project,
        "activeInputs": active_inputs,
        "candidateInputs": candidate_inputs,
        "candidateInputPaths": {
            "connectorAuthTaskId": connector_auth_task_id,
            "notificationAnchorFile": (
                str(notification_anchor_file.resolve(strict=True))
                if notification_anchor_file is not None
                else None
            ),
            "notificationProvenanceFile": (
                str(notification_provenance_file.resolve(strict=True))
                if notification_provenance_file is not None
                else None
            ),
        },
        "activeConfigDir": str(active_config_dir),
        "candidateConfigDir": str(candidate_config_dir),
        "activeSecretsDir": str(secrets_dir),
        "journalDir": str(journal_dir.resolve()),
        "candidateSelectionFile": str(promote_selection_file.resolve()),
        "selectionBackupPath": str(selection_backup_path),
        "activeSelection": selection_state,
        "runtimeConfigIdentity": {
            "active": active_mount_identity,
            "candidate": candidate_mount_identity,
        },
        "candidateServices": target_images,
        "targets": targets,
        "activeServices": active_services,
        "edgeDelivery": {
            "containerId": edge_container["containerId"],
            "imageRef": edge_container["imageRef"],
            "imageId": edge_container["imageId"],
            "revision": edge_status["revision"],
            "deadCount": edge_status["dead_count"],
        },
        "navigatorDrain": {
            "containerId": navigator_container["containerId"],
            "imageRef": navigator_container["imageRef"],
            "imageId": navigator_container["imageId"],
            "instanceId": navigator_status["instanceId"],
            "generation": navigator_status["generation"],
        },
        "database": {
            "service": "navigator-persistence" if memory_update_target is None else None,
            "volume": "navigator-sessions" if memory_update_target is None else None,
            "containerPath": DATABASE_PATH if memory_update_target is None else None,
            "backupPath": str(backup_path) if memory_update_target is None else None,
        },
        "candidatePreflight": {
            "mode": candidate_preflight["mode"],
            "phase": candidate_preflight["connectorAuthPhase"],
            "ingressExposure": ingress_exposure,
            "hostCaddyBinary": host_caddy_binary,
            "opensslBinary": openssl_binary,
            "hostFrontedIngressReadiness": candidate_preflight.get("hostFrontedIngressReadiness"),
            "credentialReadOnlyRuntimeReady": candidate_preflight["credentialReadOnlyRuntimeReady"],
            "fullCloudWriteDeploymentReady": candidate_preflight["fullCloudWriteDeploymentReady"],
            "lifecycleAcceptance": candidate_preflight["lifecycleAcceptance"],
            "expectedProviderScopeDigests": candidate_preflight["expectedProviderScopeDigests"],
            "huggingfaceRepositoryScopeDigestSha256": candidate_preflight[
                "huggingfaceRepositoryScopeDigestSha256"
            ],
            "notificationInputs": {
                key: value
                for key, value in candidate_preflight.items()
                if key.startswith("credentialNotification")
            },
            "composeCgroupMemoryLimitMiB": candidate_preflight["composeCgroupMemoryLimitMiB"],
            "composeCgroupCpuLimit": candidate_preflight["composeCgroupCpuLimit"],
            "maxMemoryMiB": max_memory_mib,
            "maxCpus": max_cpus,
            "requiredFreeGiB": required_free_gib,
            "minimumMemAvailableMiB": minimum_mem_available_mib,
        },
        "exchangeReadiness": {
            "required": _exchange_readiness_required(
                phase, image_update_target, listener_repair=listener_repair
            ),
            "status": (
                "PENDING_LIVE_READBACK"
                if _exchange_readiness_required(
                    phase, image_update_target, listener_repair=listener_repair
                )
                else "NOT_REQUIRED_PROVIDER_BOOTSTRAP"
            ),
        },
        "policy": {
            "dockerPrefix": docker_prefix,
            "updateOrder": targets,
            "noDependencies": True,
            "pull": False,
            "build": False,
            "forceRecreate": memory_update_target is None,
            **(
                {
                    "mutationMode": MEMORY_UPDATE_MUTATION_MODE,
                    "ingressMutation": "NONE",
                    "navigatorMutation": "NONE",
                    "databaseMutation": "NONE",
                }
                if memory_update_target is not None
                else {}
            ),
            "preserveNamedVolumes": True,
            "databaseRollback": (
                "IMAGE_CONFIG_ONLY_NEVER_RESTORE_SNAPSHOT"
                if memory_update_target is None
                else "NONE_NO_DATABASE_MUTATION"
            ),
            "unknownDockerMutation": "RECOVERY_REQUIRED_NO_REPLAY",
            "pauseHolder": None if memory_update_target is not None else operation_id,
            "drainTimeoutSeconds": drain_timeout_seconds,
            "healthTimeoutSeconds": health_timeout_seconds,
            "selectionPromotion": "AFTER_ALL_HEALTH_AND_RESOURCE_READBACK",
        },
    }
    if memory_update_target is not None:
        plan["memoryUpdateState"] = _memory_update_plan_state(
            target=memory_update_target,
            active_compose=active_compose,
            candidate_compose=candidate_compose,
            active_services=active_services,
            runtime_config_identity=active_mount_identity,
            active_compose_files=active_compose_files,
            active_env_files=active_env_files,
            candidate_compose_files=candidate_compose_files,
            candidate_env_files=candidate_env_files,
            selection_record=selection_state,
            journal_dir=journal_dir,
            max_memory_mib=max_memory_mib,
            max_cpus=max_cpus,
            docker_prefix=docker_prefix,
        )
    plan["planSha256"] = _sha256_bytes(_canonical_json(plan))
    _validate_plan(plan)
    try:
        runtime_update._write_private_json(plan_path, plan, require_artifact=True)
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "stage transition plan could not be persisted privately"
        ) from error
    return {"operationId": operation_id, "planSha256": plan["planSha256"]}


def _validate_plan(plan: dict[str, Any]) -> None:
    """Validate the closed private plan and its digest before any mutation."""

    required = {
        "schemaVersion",
        "operationId",
        "phase",
        "createdAtUtc",
        "project",
        "activeInputs",
        "candidateInputs",
        "candidateInputPaths",
        "activeConfigDir",
        "candidateConfigDir",
        "activeSecretsDir",
        "journalDir",
        "candidateSelectionFile",
        "selectionBackupPath",
        "activeSelection",
        "runtimeConfigIdentity",
        "candidateServices",
        "targets",
        "activeServices",
        "edgeDelivery",
        "navigatorDrain",
        "database",
        "candidatePreflight",
        "exchangeReadiness",
        "policy",
        "planSha256",
    }
    listener_repair = False
    memory_update_target = None
    if set(plan) == required:
        image_update_target = None
    elif set(plan) == required | {"imageUpdateTarget"}:
        image_update_target = plan.get("imageUpdateTarget")
        if (
            not isinstance(image_update_target, str)
            or image_update_target not in IMAGE_UPDATE_TARGETS
            or image_update_target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
            or (
                image_update_target == CLOUD_IMAGE_UPDATE_TARGET
                and plan.get("phase") != "issued-connector-activation"
            )
        ):
            image_update_target = "__invalid__"
    elif set(plan) == required | {"imageUpdateTarget", "accountCenterImageUpdateState"}:
        image_update_target = plan.get("imageUpdateTarget")
        if (
            image_update_target != ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
            or plan.get("phase") not in ACCOUNT_CENTER_IMAGE_UPDATE_PHASES
        ):
            image_update_target = "__invalid__"
    elif set(plan) == required | {"imageUpdateTarget", "cloudRunnerState"}:
        image_update_target = plan.get("imageUpdateTarget")
        if (
            image_update_target != CLOUD_IMAGE_UPDATE_TARGET
            or plan.get("phase") != "provider-bootstrap"
        ):
            image_update_target = "__invalid__"
    elif set(plan) == required | {"listenerRepairState", "cloudRunnerState"}:
        image_update_target = None
        listener_repair = True
    elif set(plan) == required | {"memoryUpdateTarget", "memoryUpdateState"}:
        image_update_target = None
        memory_update_target = plan.get("memoryUpdateTarget")
        if (
            not isinstance(memory_update_target, str)
            or memory_update_target not in MEMORY_UPDATE_TARGETS
        ):
            memory_update_target = "__invalid__"
    else:
        image_update_target = "__invalid__"
    if (
        image_update_target == "__invalid__"
        or memory_update_target == "__invalid__"
        or plan.get("schemaVersion") != PLAN_SCHEMA
    ):
        raise StageTransitionError("stage transition plan has an unsupported closed schema")
    for field in ("activeServices", "candidateServices"):
        if not isinstance(plan.get(field), dict):
            raise StageTransitionError(f"stage transition {field} record is malformed")
    if plan.get("phase") not in PHASES or not isinstance(plan.get("project"), str):
        raise StageTransitionError("stage transition plan identity is malformed")
    _validate_config_directory_policy(
        image_update_target=image_update_target,
        memory_update_target=memory_update_target,
        listener_repair=listener_repair,
        active_config_dir=plan["activeConfigDir"],
        candidate_config_dir=plan["candidateConfigDir"],
    )
    if image_update_target == CLOUD_IMAGE_UPDATE_TARGET and (
        plan.get("phase") not in {"provider-bootstrap", "issued-connector-activation"}
        or (plan.get("phase") == "provider-bootstrap" and "cloudRunnerState" not in plan)
        or (plan.get("phase") == "issued-connector-activation" and "cloudRunnerState" in plan)
    ):
        raise StageTransitionError(
            "Cloud image update plan must use its exact bootstrap or issued connector gate"
        )
    if (
        image_update_target == HUGGINGFACE_IMAGE_UPDATE_TARGET
        and plan.get("phase") != "issued-connector-activation"
    ):
        raise StageTransitionError(
            "Hugging Face image updates require issued-connector-activation and the existing connector runtime"
        )
    if (
        image_update_target == GITHUB_ISSUES_IMAGE_UPDATE_TARGET
        and plan.get("phase") != "issued-connector-activation"
    ):
        raise StageTransitionError(
            "GitHub Issues image updates require issued-connector-activation and the existing connector runtime"
        )
    if (
        image_update_target == PERSISTENCE_IMAGE_UPDATE_TARGET
        and plan.get("phase") != "issued-connector-activation"
    ):
        raise StageTransitionError(
            "Persistence image updates require issued-connector-activation and the existing connector runtime"
        )
    if (
        image_update_target == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET
        and plan.get("phase") not in WORKSPACE_AUTHORITY_IMAGE_UPDATE_PHASES
    ):
        raise StageTransitionError(
            "Workspace Authority image updates require the selected Graph bootstrap or issued activation phase"
        )
    if memory_update_target is not None and plan.get("phase") != "issued-connector-activation":
        raise StageTransitionError("memory updates require issued-connector-activation")
    if listener_repair and plan.get("phase") != "provider-bootstrap":
        raise StageTransitionError("provider listener repair is limited to provider-bootstrap")
    if plan.get("phase") == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE and (
        (
            image_update_target is not None
            and image_update_target
            not in {
                ACCOUNT_CENTER_IMAGE_UPDATE_TARGET,
                EXCHANGE_IMAGE_UPDATE_TARGET,
                WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET,
            }
        )
        or memory_update_target is not None
        or listener_repair
    ):
        raise StageTransitionError(
            "Graph bootstrap permits only its closed Account Center, Exchange, or Workspace Authority image continuation"
        )
    if plan.get("phase") == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE and "cloudRunnerState" in plan:
        raise StageTransitionError("Graph provider bootstrap must not change the cloud runner")
    if (
        not isinstance(plan.get("targets"), list)
        or not plan["targets"]
        or any(not isinstance(name, str) for name in plan["targets"])
        or len(set(plan["targets"])) != len(plan["targets"])
    ):
        raise StageTransitionError("stage transition plan lacks its ordered service targets")
    expected_target_order = (
        OFFICE_GRAPH_STAGE_TARGET_ORDER
        if plan.get("phase") == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
        else SERVICE_ORDER
    )
    if (
        memory_update_target is None
        and image_update_target is None
        and plan["targets"]
        != [name for name in expected_target_order if name in set(plan["targets"])]
    ):
        raise StageTransitionError("stage transition targets are not in canonical dependency order")
    if image_update_target is not None and plan["targets"] != [image_update_target]:
        raise StageTransitionError("image update plan must contain exactly its selected target")
    if memory_update_target is not None and plan["targets"] != [memory_update_target]:
        raise StageTransitionError("memory update plan must contain exactly its selected target")
    if listener_repair and plan["targets"] != list(PROVIDER_LISTENER_REPAIR_TARGETS):
        raise StageTransitionError("listener repair plan must contain exactly its two providers")
    active_runtime_services = set(plan.get("activeServices", {}))
    candidate_runtime_services = set(plan.get("candidateServices", {}))
    if plan.get("phase") == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE and image_update_target is None:
        expected_targets = {
            "account-center",
            "exchange",
            "navigator",
            "navigator-persistence",
            "teams-provider",
            "teams-ingress",
            "microsoft-graph",
            "cloud-management-phase2",
        }
        expected_ordered_targets = [
            name for name in OFFICE_GRAPH_STAGE_TARGET_ORDER if name in expected_targets
        ]
        if (
            active_runtime_services != RUNTIME_SERVICE_SET
            or candidate_runtime_services != OFFICE_GRAPH_RUNTIME_SERVICE_SET
            or plan["targets"] != expected_ordered_targets
            or plan["candidatePreflight"].get("composeCgroupMemoryLimitMiB") != 3584
            or plan["candidatePreflight"].get("composeCgroupCpuLimit") != 1.95
            or plan["candidatePreflight"].get("maxMemoryMiB") != 3584
            or plan["candidatePreflight"].get("maxCpus") != 1.95
        ):
            raise StageTransitionError(
                "Graph bootstrap plan must retain 14 services, add Graph, and update the closed image set"
            )
        for service_name, row in plan["candidateServices"].items():
            if (
                not isinstance(row, dict)
                or set(row) != {"active", "candidate", "added", "configChanged"}
                or not isinstance(row.get("candidate"), dict)
            ):
                raise StageTransitionError(
                    f"Graph bootstrap target record is malformed for {service_name}"
                )
            if service_name == "microsoft-graph":
                if row.get("active") is not None or row.get("added") is not True:
                    raise StageTransitionError("Graph bootstrap plan must add the Graph image")
            elif service_name == "cloud-management-phase2":
                if (
                    not isinstance(row.get("active"), dict)
                    or row.get("candidate") != row.get("active")
                    or row.get("added") is not False
                    or row.get("configChanged") is not True
                ):
                    raise StageTransitionError(
                        "Graph bootstrap must keep the Cloud image and record its memory-only change"
                    )
            elif service_name in expected_targets:
                if (
                    not isinstance(row.get("active"), dict)
                    or row.get("added") is not False
                    or row["active"] == row["candidate"]
                ):
                    raise StageTransitionError(
                        f"Graph bootstrap must update the pinned {service_name} image"
                    )
            elif (
                row.get("active") != row.get("candidate")
                or row.get("added") is not False
                or row.get("configChanged") is not False
            ):
                raise StageTransitionError(
                    f"Graph bootstrap changes unselected runtime service {service_name}"
                )
    if image_update_target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET:
        candidate_preflight = plan.get("candidatePreflight")
        if (
            active_runtime_services != ACCOUNT_CENTER_IMAGE_UPDATE_SERVICE_SET
            or candidate_runtime_services != ACCOUNT_CENTER_IMAGE_UPDATE_SERVICE_SET
            or plan.get("targets") != [ACCOUNT_CENTER_IMAGE_UPDATE_TARGET]
            or not isinstance(candidate_preflight, dict)
            or candidate_preflight.get("composeCgroupMemoryLimitMiB") != 3584
            or candidate_preflight.get("composeCgroupCpuLimit") != 1.95
            or candidate_preflight.get("maxMemoryMiB") != 3584
            or candidate_preflight.get("maxCpus") != 1.95
        ):
            raise StageTransitionError(
                "Account Center image update must preserve the exact 15-service Graph runtime and budgets"
            )
        for service_name, row in plan["candidateServices"].items():
            if (
                not isinstance(row, dict)
                or set(row) != {"active", "candidate", "added", "configChanged"}
                or not isinstance(row.get("active"), dict)
                or not isinstance(row.get("candidate"), dict)
                or row.get("added") is not False
                or row.get("configChanged") is not False
                or (
                    service_name == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
                    and (
                        row.get("active") == row.get("candidate")
                        or set(row["active"]) != {"imageRef", "imageId"}
                        or set(row["candidate"]) != {"imageRef", "imageId"}
                        or not isinstance(row["active"].get("imageRef"), str)
                        or not isinstance(row["candidate"].get("imageRef"), str)
                        or row["active"].get("imageRef") == row["candidate"].get("imageRef")
                        or row["active"].get("imageId") == row["candidate"].get("imageId")
                        or not isinstance(row["active"].get("imageId"), str)
                        or IMAGE_ID_RE.fullmatch(row["active"]["imageId"]) is None
                        or not isinstance(row["candidate"].get("imageId"), str)
                        or IMAGE_ID_RE.fullmatch(row["candidate"]["imageId"]) is None
                    )
                )
                or (
                    service_name != ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
                    and row.get("active") != row.get("candidate")
                )
            ):
                raise StageTransitionError(
                    f"Account Center image update changes an unselected image: {service_name}"
                )
        _validate_account_center_image_update_state(plan.get("accountCenterImageUpdateState"))
        account_center_state = plan["accountCenterImageUpdateState"]
        preserved_unknown = account_center_state.get("preservedUnknown")
        evidence_record = plan["candidateInputs"].get("accountCenterUnknownPreservationEvidence")
        if preserved_unknown is None:
            if evidence_record is not None:
                raise StageTransitionError(
                    "Account Center UNKNOWN evidence is present without explicit plan opt-in"
                )
        elif not isinstance(evidence_record, dict) or evidence_record.get(
            "sha256"
        ) != preserved_unknown.get("operatorEvidenceSha256"):
            raise StageTransitionError(
                "Account Center preserved UNKNOWN state is not bound to its private evidence input"
            )
    if image_update_target == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET:
        candidate_preflight = plan.get("candidatePreflight")
        if (
            plan.get("phase") not in WORKSPACE_AUTHORITY_IMAGE_UPDATE_PHASES
            or active_runtime_services != OFFICE_GRAPH_RUNTIME_SERVICE_SET
            or candidate_runtime_services != OFFICE_GRAPH_RUNTIME_SERVICE_SET
            or plan.get("targets") != [WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET]
            or not isinstance(candidate_preflight, dict)
            or candidate_preflight.get("composeCgroupMemoryLimitMiB") != 3584
            or candidate_preflight.get("composeCgroupCpuLimit") != 1.95
            or candidate_preflight.get("maxMemoryMiB") != 3584
            or candidate_preflight.get("maxCpus") != 1.95
        ):
            raise StageTransitionError(
                "Workspace Authority image update must preserve the exact 15-service Graph runtime and budgets"
            )
        for service_name, row in plan["candidateServices"].items():
            if (
                not isinstance(row, dict)
                or set(row) != {"active", "candidate", "added", "configChanged"}
                or not isinstance(row.get("active"), dict)
                or not isinstance(row.get("candidate"), dict)
                or set(row["active"]) != {"imageRef", "imageId"}
                or set(row["candidate"]) != {"imageRef", "imageId"}
                or row.get("added") is not False
                or row.get("configChanged") is not False
                or (
                    service_name == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET
                    and (
                        row["active"] == row["candidate"]
                        or row["active"].get("imageRef") == row["candidate"].get("imageRef")
                        or row["active"].get("imageId") == row["candidate"].get("imageId")
                    )
                )
                or (
                    service_name != WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET
                    and row["active"] != row["candidate"]
                )
            ):
                raise StageTransitionError(
                    f"Workspace Authority image update changes an unselected image: {service_name}"
                )
    if (
        image_update_target == EXCHANGE_IMAGE_UPDATE_TARGET
        and plan.get("phase") == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
    ):
        candidate_preflight = plan.get("candidatePreflight")
        if (
            active_runtime_services != OFFICE_GRAPH_RUNTIME_SERVICE_SET
            or candidate_runtime_services != OFFICE_GRAPH_RUNTIME_SERVICE_SET
            or plan.get("targets") != [EXCHANGE_IMAGE_UPDATE_TARGET]
            or not isinstance(candidate_preflight, dict)
            or candidate_preflight.get("composeCgroupMemoryLimitMiB") != 3584
            or candidate_preflight.get("composeCgroupCpuLimit") != 1.95
            or candidate_preflight.get("maxMemoryMiB") != 3584
            or candidate_preflight.get("maxCpus") != 1.95
        ):
            raise StageTransitionError(
                "Exchange Graph-bootstrap image update must preserve the exact 15-service topology and budget"
            )
        for service_name, row in plan["candidateServices"].items():
            if (
                not isinstance(row, dict)
                or set(row) != {"active", "candidate", "added", "configChanged"}
                or not isinstance(row.get("active"), dict)
                or not isinstance(row.get("candidate"), dict)
                or set(row["active"]) != {"imageRef", "imageId"}
                or set(row["candidate"]) != {"imageRef", "imageId"}
                or row.get("added") is not False
                or row.get("configChanged") is not False
                or (
                    service_name == EXCHANGE_IMAGE_UPDATE_TARGET
                    and (
                        row["active"] == row["candidate"]
                        or row["active"].get("imageRef") == row["candidate"].get("imageRef")
                        or row["active"].get("imageId") == row["candidate"].get("imageId")
                    )
                )
                or (
                    service_name != EXCHANGE_IMAGE_UPDATE_TARGET
                    and row["active"] != row["candidate"]
                )
            ):
                raise StageTransitionError(
                    f"Exchange Graph-bootstrap image update changes an unselected image: {service_name}"
                )
    if listener_repair:
        if (
            _runtime_service_shape(plan["activeServices"], "listener repair active plan")
            != _runtime_service_shape(plan["candidateServices"], "listener repair candidate plan")
            or active_runtime_services != candidate_runtime_services
        ):
            raise StageTransitionError(
                "listener repair plan must retain the exact runtime inventory"
            )
        for service_name, row in plan["candidateServices"].items():
            if (
                not isinstance(row, dict)
                or set(row) != {"active", "candidate", "added", "configChanged"}
                or not isinstance(row.get("active"), dict)
                or row.get("candidate") != row.get("active")
                or row.get("added") is not False
                or row.get("configChanged") is not False
            ):
                raise StageTransitionError(
                    f"listener repair plan changes the immutable image for {service_name}"
                )
    if memory_update_target is not None:
        if (
            _runtime_service_shape(plan["activeServices"], "memory update active plan")
            != _runtime_service_shape(plan["candidateServices"], "memory update candidate plan")
            or active_runtime_services != candidate_runtime_services
        ):
            raise StageTransitionError("memory update plan must retain the exact runtime inventory")
        for service_name, row in plan["candidateServices"].items():
            if (
                not isinstance(row, dict)
                or set(row) != {"active", "candidate", "added", "configChanged"}
                or not isinstance(row.get("active"), dict)
                or row.get("candidate") != row.get("active")
                or row.get("added") is not False
                or row.get("configChanged") is not (service_name == memory_update_target)
            ):
                raise StageTransitionError(
                    f"memory update plan changes the image or unselected service {service_name}"
                )
        memory_state = plan.get("memoryUpdateState")
        expected_memory_state_fields = {
            "activeMemoryBytes",
            "candidateMemoryBytes",
            "activeComposeConfigHash",
            "candidateComposeConfigHash",
            "containerComposeConfigHash",
            "configHashRelation",
            "configHashProofOperationId",
            "configHashProofPlanSha256",
            "containerConfigSha256",
            "hostConfigSha256WithoutMemory",
            "processId",
            "processStartedAt",
            "restartCount",
            "activeRuntimeMemoryBytes",
            "activeRuntimeNanoCpus",
        }
        active_target = plan["activeServices"].get(memory_update_target)
        if (
            not isinstance(memory_state, dict)
            or set(memory_state) != expected_memory_state_fields
            or not isinstance(active_target, dict)
            or any(
                isinstance(memory_state.get(field), bool)
                or not isinstance(memory_state.get(field), int)
                or memory_state[field] <= 0
                for field in (
                    "activeMemoryBytes",
                    "candidateMemoryBytes",
                    "processId",
                    "activeRuntimeMemoryBytes",
                    "activeRuntimeNanoCpus",
                )
            )
            or memory_state["candidateMemoryBytes"] <= memory_state["activeMemoryBytes"]
            or isinstance(memory_state.get("restartCount"), bool)
            or not isinstance(memory_state.get("restartCount"), int)
            or memory_state["restartCount"] < 0
            or not isinstance(memory_state.get("processStartedAt"), str)
            or not memory_state["processStartedAt"].strip()
            or not isinstance(active_target.get("containerId"), str)
            or FULL_CONTAINER_ID_RE.fullmatch(active_target["containerId"]) is None
            or not isinstance(active_target.get("imageId"), str)
            or IMAGE_ID_RE.fullmatch(active_target["imageId"]) is None
            or any(
                not isinstance(memory_state.get(field), str)
                or SHA256_RE.fullmatch(memory_state[field]) is None
                for field in (
                    "activeComposeConfigHash",
                    "candidateComposeConfigHash",
                    "containerComposeConfigHash",
                    "containerConfigSha256",
                    "hostConfigSha256WithoutMemory",
                )
            )
            or memory_state["activeComposeConfigHash"] == memory_state["candidateComposeConfigHash"]
            or memory_state.get("configHashRelation")
            not in {"MATCHES_ACTIVE", "SELECTOR_JOURNAL_PROVEN_STALE"}
            or (
                memory_state["configHashRelation"] == "MATCHES_ACTIVE"
                and (
                    memory_state["containerComposeConfigHash"]
                    != memory_state["activeComposeConfigHash"]
                    or memory_state.get("configHashProofOperationId") is not None
                    or memory_state.get("configHashProofPlanSha256") is not None
                )
            )
            or (
                memory_state["configHashRelation"] == "SELECTOR_JOURNAL_PROVEN_STALE"
                and (
                    memory_state["containerComposeConfigHash"]
                    == memory_state["activeComposeConfigHash"]
                    or not isinstance(memory_state.get("configHashProofOperationId"), str)
                    or not isinstance(memory_state.get("configHashProofPlanSha256"), str)
                    or SHA256_RE.fullmatch(memory_state["configHashProofPlanSha256"]) is None
                )
            )
        ):
            raise StageTransitionError(
                "memory update target identity and no-swap limits are malformed"
            )
        if memory_state["configHashRelation"] == "SELECTOR_JOURNAL_PROVEN_STALE":
            try:
                if (
                    str(uuid.UUID(memory_state["configHashProofOperationId"]))
                    != memory_state["configHashProofOperationId"]
                ):
                    raise ValueError("non-canonical operation ID")
            except ValueError as error:
                raise StageTransitionError(
                    "memory update config-hash proof reference is malformed"
                ) from error
        policy = plan.get("policy")
        if (
            not isinstance(policy, dict)
            or policy.get("mutationMode") != MEMORY_UPDATE_MUTATION_MODE
            or policy.get("forceRecreate") is not False
            or policy.get("ingressMutation") != "NONE"
            or policy.get("navigatorMutation") != "NONE"
            or policy.get("databaseMutation") != "NONE"
            or policy.get("updateOrder") != [memory_update_target]
            or policy.get("noDependencies") is not True
            or policy.get("pull") is not False
            or policy.get("build") is not False
            or policy.get("preserveNamedVolumes") is not True
            or policy.get("databaseRollback") != "NONE_NO_DATABASE_MUTATION"
            or policy.get("pauseHolder") is not None
            or policy.get("selectionPromotion") != "AFTER_ALL_HEALTH_AND_RESOURCE_READBACK"
            or policy.get("unknownDockerMutation") != "RECOVERY_REQUIRED_NO_REPLAY"
            or not isinstance(plan.get("database"), dict)
            or plan["database"].get("service") is not None
            or plan["database"].get("volume") is not None
            or plan["database"].get("containerPath") is not None
            or plan["database"].get("backupPath") is not None
        ):
            raise StageTransitionError("memory update plan permits an unsafe mutation")
    if "cloudRunnerState" in plan or listener_repair:
        _validate_cloud_runner_state_record(plan.get("cloudRunnerState"), plan["activeServices"])
    if listener_repair:
        _validate_listener_repair_state_record(
            plan.get("listenerRepairState"), plan["activeServices"]
        )
    added_services = {
        name
        for name, row in plan.get("candidateServices", {}).items()
        if isinstance(row, dict) and row.get("added") is True
    }
    runtime_identity = plan.get("runtimeConfigIdentity")
    if not isinstance(runtime_identity, dict) or set(runtime_identity) != {"active", "candidate"}:
        raise StageTransitionError("runtime configuration identity is malformed")
    _validate_runtime_config_identity(
        runtime_identity.get("active"),
        expected_services=active_runtime_services,
        label="active runtime configuration identity",
    )
    _validate_runtime_config_identity(
        runtime_identity.get("candidate"),
        expected_services=active_runtime_services | added_services,
        label="candidate runtime configuration identity",
    )
    if not isinstance(plan.get("planSha256"), str) or not SHA256_RE.fullmatch(plan["planSha256"]):
        raise StageTransitionError("stage transition plan digest is malformed")
    unsigned = dict(plan)
    expected_digest = unsigned.pop("planSha256")
    if _sha256_bytes(_canonical_json(unsigned)) != expected_digest:
        raise StageTransitionError("stage transition plan digest does not match its contents")
    for field in ("activeInputs", "candidateInputs"):
        if not isinstance(plan.get(field), dict):
            raise StageTransitionError(f"stage transition {field} record is malformed")
    if listener_repair:
        _validate_listener_repair_selection_contract(
            phase=plan["phase"],
            project=plan["project"],
            selection_record=plan["activeSelection"],
            selection_path=plan["candidateSelectionFile"],
            active_config_dir=plan["activeConfigDir"],
            candidate_config_dir=plan["candidateConfigDir"],
            active_secrets_dir=plan["activeSecretsDir"],
            active_inputs=plan["activeInputs"],
            candidate_inputs=plan["candidateInputs"],
            runtime_config_identity=runtime_identity,
        )
    else:
        _validate_image_update_selection_contract(
            target=image_update_target,
            memory_update_target=memory_update_target,
            phase=plan["phase"],
            project=plan["project"],
            selection_record=plan["activeSelection"],
            selection_path=plan["candidateSelectionFile"],
            active_config_dir=plan["activeConfigDir"],
            candidate_config_dir=plan["candidateConfigDir"],
            active_secrets_dir=plan["activeSecretsDir"],
            active_inputs=plan["activeInputs"],
            candidate_inputs=plan["candidateInputs"],
            runtime_config_identity=runtime_identity,
        )
    readiness = plan.get("candidatePreflight")
    if (
        not isinstance(readiness, dict)
        or readiness.get("mode") != "connector-auth-readonly"
        or readiness.get("ingressExposure") not in {"public-cutover", "host-fronted-staging"}
        or not isinstance(readiness.get("hostCaddyBinary"), str)
        or not readiness["hostCaddyBinary"]
        or not isinstance(readiness.get("opensslBinary"), str)
        or not readiness["opensslBinary"]
        or readiness.get("credentialReadOnlyRuntimeReady") is not True
        or readiness.get("fullCloudWriteDeploymentReady") is not False
        or readiness.get("lifecycleAcceptance") != "NOT_RUN"
    ):
        raise StageTransitionError("stage transition candidate readiness record is malformed")
    if memory_update_target is not None and (
        isinstance(readiness.get("maxMemoryMiB"), bool)
        or not isinstance(readiness.get("maxMemoryMiB"), int)
        or readiness["maxMemoryMiB"] > 3584
        or isinstance(readiness.get("maxCpus"), bool)
        or not isinstance(readiness.get("maxCpus"), (int, float))
        or readiness["maxCpus"] > 1.95
        or isinstance(readiness.get("requiredFreeGiB"), bool)
        or not isinstance(readiness.get("requiredFreeGiB"), (int, float))
        or readiness["requiredFreeGiB"] < 2.0
        or isinstance(readiness.get("minimumMemAvailableMiB"), bool)
        or not isinstance(readiness.get("minimumMemAvailableMiB"), int)
        or readiness["minimumMemAvailableMiB"] < MEMORY_RESERVE_DEFAULT_MIB
        or plan["memoryUpdateState"]["activeRuntimeMemoryBytes"]
        > readiness["maxMemoryMiB"] * preflight.MIB
        or plan["memoryUpdateState"]["activeRuntimeNanoCpus"]
        > int(Decimal(str(readiness["maxCpus"])) * 1_000_000_000)
    ):
        raise StageTransitionError(
            "memory update plan weakens the canonical host resource safeguards"
        )
    host_fronted_readiness = readiness.get("hostFrontedIngressReadiness")
    if readiness["ingressExposure"] == "host-fronted-staging":
        if (
            not isinstance(host_fronted_readiness, dict)
            or host_fronted_readiness.get("schemaVersion")
            != "cyrene-host-fronted-ingress-readiness-v1"
            or host_fronted_readiness.get("status") != "HOST_FRONTED_STAGING_READY"
            or host_fronted_readiness.get("ingressExposure") != "host-fronted-staging"
            or host_fronted_readiness.get("accepted") is not False
            or host_fronted_readiness.get("deploymentReady") is not False
            or host_fronted_readiness.get("publicCallbackPostApply") != "NOT_RUN"
            or host_fronted_readiness.get("backendTls") != "PASS"
            or host_fronted_readiness.get("backendUnsignedCallbackHttpStatus") != 401
            or host_fronted_readiness.get("publicTls") != "PASS"
            or host_fronted_readiness.get("publicUnsignedCallbackHttpStatus") != 401
            or any(
                not isinstance(host_fronted_readiness.get(field), str)
                or SHA256_RE.fullmatch(host_fronted_readiness[field]) is None
                for field in (
                    "activeCaddyfileSha256",
                    "caddyBinarySha256",
                    "backendCaSha256",
                )
            )
            or not isinstance(host_fronted_readiness.get("ingressImageId"), str)
            or IMAGE_ID_RE.fullmatch(host_fronted_readiness["ingressImageId"]) is None
        ):
            raise StageTransitionError("host-fronted ingress readiness record is malformed")
    elif host_fronted_readiness is not None:
        raise StageTransitionError("public-cutover plan must not contain host-fronted readiness")
    exchange_readiness = plan.get("exchangeReadiness")
    exchange_readiness_required = _exchange_readiness_required(
        plan["phase"], image_update_target, listener_repair=listener_repair
    )
    if (
        not isinstance(exchange_readiness, dict)
        or set(exchange_readiness) != {"required", "status"}
        or not isinstance(exchange_readiness.get("required"), bool)
        or exchange_readiness["required"] != exchange_readiness_required
        or exchange_readiness.get("status")
        != (
            "PENDING_LIVE_READBACK"
            if exchange_readiness_required
            else "NOT_REQUIRED_PROVIDER_BOOTSTRAP"
        )
    ):
        raise StageTransitionError("Exchange live readiness plan record is malformed")


def _verify_record(record: Any, label: str) -> None:
    """Require a frozen file input to retain its exact canonical path and digest."""

    if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
        raise StageTransitionError(f"{label} plan record is malformed")
    path_text = record.get("path")
    expected_digest = record.get("sha256")
    if (
        not isinstance(path_text, str)
        or not isinstance(expected_digest, str)
        or SHA256_RE.fullmatch(expected_digest) is None
    ):
        raise StageTransitionError(f"{label} plan record is malformed")
    path = Path(path_text)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise StageTransitionError(f"{label} disappeared after planning") from error
    if str(resolved) != path_text or _sha256_file(resolved) != expected_digest:
        raise StageTransitionError(f"{label} changed after planning; create a new plan")


def _verify_file_group(records: Any, label: str) -> None:
    """Recheck an ordered path/digest list from one immutable plan group."""

    if not isinstance(records, list) or not records:
        raise StageTransitionError(f"{label} file group is missing")
    for index, record in enumerate(records):
        _verify_record(record, f"{label} {index + 1}")


def _verify_directory_group(path_text: Any, records: Any, label: str) -> None:
    """Recheck a complete private directory manifest without changing any file."""

    if not isinstance(path_text, str) or not isinstance(records, list):
        raise StageTransitionError(f"{label} directory fingerprint is malformed")
    path = _validate_private_directory(Path(path_text), label, expected_uid=10001)
    if _directory_manifest(path, label) != records:
        raise StageTransitionError(f"{label} directory changed after planning; create a new plan")


def _verify_all_inputs(plan: dict[str, Any]) -> None:
    """Recheck every active and candidate input before any pause or Docker mutation."""

    active = plan["activeInputs"]
    candidate = plan["candidateInputs"]
    for side, rows in (("active", active), ("candidate", candidate)):
        _verify_file_group(rows.get("composeFiles"), f"{side} Compose")
        _verify_file_group(rows.get("envFiles"), f"{side} env")
        _verify_record(rows.get("imageLock"), f"{side} image lock")
        config_path = plan["activeConfigDir"] if side == "active" else plan["candidateConfigDir"]
        _verify_directory_group(config_path, rows.get("configDirectoryFiles"), f"{side} config")
    _verify_secret_manifest(active.get("secretFiles"), "active")
    _verify_runtime_config_identity_sources(
        plan["runtimeConfigIdentity"]["active"], "active runtime configuration"
    )
    _verify_runtime_config_identity_sources(
        plan["runtimeConfigIdentity"]["candidate"], "candidate runtime configuration"
    )
    for key, value in candidate.items():
        if key in {
            "composeFiles",
            "envFiles",
            "imageLock",
            "configDirectoryFiles",
            "secretFiles",
        }:
            continue
        if key == "accountCenterUnknownPreservationEvidence":
            _verify_operator_private_file_record(
                value, "Account Center unknown preservation evidence"
            )
            continue
        if value is None:
            continue
        if isinstance(value, dict) and "path" in value:
            _verify_record(value, f"candidate {key}")
        elif isinstance(value, list):
            _verify_file_group(value, f"candidate {key}")
        else:
            raise StageTransitionError(f"candidate {key} fingerprint is malformed")
    _verify_secret_manifest(candidate.get("secretFiles"), "candidate")


def _check_selection_unchanged(plan: dict[str, Any]) -> None:
    """Require the stable runtime selector to remain at its planned prior revision."""

    record = plan.get("activeSelection")
    if not isinstance(record, dict) or set(record) != {"path", "sha256", "document"}:
        raise StageTransitionError("active runtime selection plan record is malformed")
    path = Path(record["path"])
    if path.is_symlink():
        raise StageTransitionError("runtime selection path became a symlink")
    current_digest = _sha256_file(path) if path.exists() else None
    if current_digest != record.get("sha256"):
        raise StageTransitionError("active runtime selection changed after planning")


def _candidate_preflight(plan: dict[str, Any]) -> dict[str, Any]:
    """Repeat static and read-only identity gates against the frozen candidate before mutation."""

    paths = plan.get("candidateInputPaths")
    readiness = plan.get("candidatePreflight")
    inputs = plan.get("candidateInputs")
    if (
        not isinstance(paths, dict)
        or not isinstance(readiness, dict)
        or not isinstance(inputs, dict)
    ):
        raise StageTransitionError("candidate preflight input record is malformed")
    phase = plan["phase"]
    evidence = inputs["connectorAuthEvidence"]
    seed = inputs["huggingfaceSeed"]
    key_file = inputs.get("runpodKey")
    try:
        result = preflight.run_preflight(
            mode="connector-auth-readonly",
            compose_files=[Path(row["path"]) for row in inputs["composeFiles"]],
            env_files=[Path(row["path"]) for row in inputs["envFiles"]],
            image_lock=Path(inputs["imageLock"]["path"]),
            secrets_dir=Path(plan["activeSecretsDir"]),
            config_dir=Path(plan["candidateConfigDir"]),
            github_hf_evidence=None,
            azure_gcp_ledger=None,
            runpod_ledger=None,
            azure_gcp_api_evidence=None,
            runpod_api_evidence=None,
            docker_prefix=plan["policy"]["dockerPrefix"],
            max_memory_mib=readiness["maxMemoryMiB"],
            max_cpus=readiness["maxCpus"],
            required_free_gib=readiness["requiredFreeGiB"],
            minimum_mem_available_mib=readiness["minimumMemAvailableMiB"],
            connector_auth_phase=phase,
            connector_auth_evidence=Path(evidence["path"]),
            connector_auth_task_id=paths["connectorAuthTaskId"],
            huggingface_seed_file=Path(seed["path"]),
            candidate_runpod_key_file=(
                Path(key_file["path"]) if isinstance(key_file, dict) else None
            ),
            notification_anchor_file=(
                Path(paths["notificationAnchorFile"])
                if paths.get("notificationAnchorFile") is not None
                else None
            ),
            notification_provenance_file=(
                Path(paths["notificationProvenanceFile"])
                if paths.get("notificationProvenanceFile") is not None
                else None
            ),
            ingress_exposure=readiness["ingressExposure"],
            host_caddy_binary=readiness["hostCaddyBinary"],
            openssl_binary=readiness["opensslBinary"],
        )
    except (OSError, preflight.PreflightError, BuildError, subprocess.SubprocessError) as error:
        raise StageTransitionError("fresh connector auth read-only preflight failed") from error
    if (
        result.get("credentialReadOnlyRuntimeReady") is not True
        or result.get("fullCloudWriteDeploymentReady") is not False
        or result.get("lifecycleAcceptance") != "NOT_RUN"
        or result.get("expectedProviderScopeDigests")
        != readiness.get("expectedProviderScopeDigests")
        or result.get("huggingfaceRepositoryScopeDigestSha256")
        != readiness.get("huggingfaceRepositoryScopeDigestSha256")
    ):
        raise StageTransitionError(
            "candidate read-only profile or provider scope changed after planning"
        )
    expected_notification = readiness.get("notificationInputs", {})
    observed_notification = {
        key: value for key, value in result.items() if key.startswith("credentialNotification")
    }
    if observed_notification != expected_notification:
        raise StageTransitionError("notification anchor/profile changed after planning")
    planned_host_readiness = readiness.get("hostFrontedIngressReadiness")
    current_host_readiness = result.get("hostFrontedIngressReadiness")
    if planned_host_readiness is None:
        if current_host_readiness is not None:
            raise StageTransitionError("candidate ingress exposure changed after planning")
    elif not isinstance(current_host_readiness, dict) or {
        key: value for key, value in current_host_readiness.items() if key != "checkedAtUtc"
    } != {key: value for key, value in planned_host_readiness.items() if key != "checkedAtUtc"}:
        raise StageTransitionError("live host-fronted readiness changed after planning")
    return result


def _candidate_config_json(compose: dict[str, Any], name: str) -> dict[str, Any]:
    """Read one validated private Compose config by its rendered host source."""

    row = compose.get("configs", {}).get(name)
    path_text = row.get("file") if isinstance(row, dict) else None
    if not isinstance(path_text, str):
        raise StageTransitionError(f"candidate {name} config is unavailable")
    try:
        document = json.loads(
            Path(path_text).read_text(encoding="utf-8"),
            object_pairs_hook=preflight._unique_json_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise StageTransitionError(f"candidate {name} config is invalid") from error
    if not isinstance(document, dict):
        raise StageTransitionError(f"candidate {name} config is malformed")
    return document


def _sorted_exchange_bindings(value: Any) -> list[dict[str, Any]]:
    """Normalize only array ordering in the official Exchange binding projection."""

    if not isinstance(value, list):
        raise StageTransitionError("Exchange readiness bindings are malformed")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"bindingId", "connectionRef", "providerId", "profileRef", "tools"}
            or any(
                not isinstance(item.get(field), str) or not item[field]
                for field in ("bindingId", "connectionRef", "providerId", "profileRef")
            )
            or item["bindingId"] in seen
            or not isinstance(item.get("tools"), list)
            or not item["tools"]
        ):
            raise StageTransitionError("Exchange readiness binding row is malformed")
        seen.add(item["bindingId"])
        tools = item["tools"]
        if any(
            not isinstance(tool, dict)
            or set(tool) != {"toolId", "operationId", "resourceProjection", "mutating"}
            or any(
                not isinstance(tool.get(field), str) or not tool[field]
                for field in ("toolId", "operationId", "resourceProjection")
            )
            or not isinstance(tool.get("mutating"), bool)
            for tool in tools
        ):
            raise StageTransitionError("Exchange readiness operation map is malformed")
        rows.append({**item, "tools": sorted(tools, key=lambda row: row["toolId"])})
    return sorted(rows, key=lambda row: row["bindingId"])


def _sorted_exchange_catalog(value: Any) -> dict[str, Any]:
    """Normalize tool catalog array ordering while retaining schemas byte-for-byte."""

    if (
        not isinstance(value, dict)
        or set(value) != {"schemaVersion", "items"}
        or value.get("schemaVersion") != 1
        or isinstance(value.get("schemaVersion"), bool)
        or not isinstance(value.get("items"), list)
    ):
        raise StageTransitionError("Exchange readiness tool catalog is malformed")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value["items"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {"bindingId", "capabilityId", "providerId", "connectionRef", "profileRef", "tools"}
            or any(
                not isinstance(item.get(field), str) or not item[field]
                for field in (
                    "bindingId",
                    "capabilityId",
                    "providerId",
                    "connectionRef",
                    "profileRef",
                )
            )
            or item.get("capabilityId") != "tool.provider.v1"
            or item["bindingId"] in seen
            or not isinstance(item.get("tools"), list)
            or not item["tools"]
        ):
            raise StageTransitionError("Exchange readiness catalog row is malformed")
        seen.add(item["bindingId"])
        tools = item["tools"]
        if any(
            not isinstance(tool, dict)
            or set(tool) != {"id", "name", "description", "readOnly", "inputSchema"}
            or any(
                not isinstance(tool.get(field), str) or not tool[field]
                for field in ("id", "name", "description")
            )
            or not isinstance(tool.get("readOnly"), bool)
            or not isinstance(tool.get("inputSchema"), dict)
            for tool in tools
        ):
            raise StageTransitionError("Exchange readiness catalog tool is malformed")
        rows.append({**item, "tools": sorted(tools, key=lambda row: row["id"])})
    return {"schemaVersion": 1, "items": sorted(rows, key=lambda row: row["bindingId"])}


def _assert_exchange_readiness_safe_shape(snapshot: dict[str, Any]) -> None:
    """Enforce credential-free readiness DTOs, including the flattened Office v2 metadata."""

    allowed_provider_ids = {
        "github",
        "huggingface",
        "azure",
        "gcp",
        "runpod",
        "computer",
        "microsoft_graph",
    }
    profiles = snapshot.get("profiles")
    connections = snapshot.get("connections")
    if not isinstance(profiles, list) or not isinstance(connections, list):
        raise StageTransitionError("Exchange readiness profiles/connections are malformed")
    profile_keys: set[tuple[str, str]] = set()
    for row in profiles:
        if not isinstance(row, dict):
            raise StageTransitionError("Exchange readiness profile row is malformed")
        provider = row.get("providerId")
        profile_ref = row.get("profileRef")
        if (
            not isinstance(provider, str)
            or provider not in allowed_provider_ids
            or not isinstance(profile_ref, str)
            or not profile_ref
        ):
            raise StageTransitionError("Exchange readiness profile identity is malformed")
        if provider in {"github", "huggingface"}:
            required = {
                "profileRef",
                "profileAlias",
                "providerId",
                "authStatus",
                "accessMode",
                "targetScope",
            }
            optional = {"authMode", "credentialRevision"}
        elif provider in {"azure", "gcp", "runpod"}:
            required = {
                "profileRef",
                "profileAlias",
                "providerId",
                "accessMode",
                "targetScope",
                "readOnly",
            }
            optional = {"authMode"}
        elif provider == "microsoft_graph":
            required = {
                "profileRef",
                "profileAlias",
                "providerId",
                "authStatus",
                "accessMode",
                "targetScope",
                "authMode",
                "credentialRevision",
                "externalSubject",
                "scopeSchemaVersion",
                "enabledFeatures",
                "requiredOAuthScopes",
                "targetRefCount",
                "credentialExpiresAt",
                "refreshExpiresAt",
                "reauthRequired",
                "reauthReasonCode",
            }
            optional = set()
        else:
            required = {"profileRef", "profileAlias", "providerId", "accessMode", "targetScope"}
            optional = set()
        if set(row) - required - optional or not required <= set(row):
            raise StageTransitionError("Exchange readiness profile contains unsupported fields")
        if row.get("profileAlias") is not None and not isinstance(row["profileAlias"], str):
            raise StageTransitionError("Exchange readiness profile alias is malformed")
        if not isinstance(row.get("accessMode"), str) or not row["accessMode"]:
            raise StageTransitionError("Exchange readiness profile access mode is malformed")
        if not isinstance(row.get("targetScope"), dict):
            raise StageTransitionError("Exchange readiness profile target scope is malformed")
        if provider in {"github", "huggingface"} and (
            not isinstance(row.get("authStatus"), str) or not row["authStatus"]
        ):
            raise StageTransitionError("Exchange readiness account profile status is malformed")
        if provider == "microsoft_graph" and (
            row.get("authStatus")
            not in {"healthy", "reauthorization_required", "revoked", "unavailable", "unknown"}
            or row.get("authMode") != "delegated_oauth"
            or row.get("accessMode") != "actor_owned"
            or not isinstance(row.get("externalSubject"), str)
            or not re.fullmatch(r"[0-9a-fA-F-]{36}:[0-9a-fA-F-]{36}", row["externalSubject"])
            or row.get("scopeSchemaVersion") != 2
            or isinstance(row.get("scopeSchemaVersion"), bool)
            or not isinstance(row.get("credentialRevision"), int)
            or isinstance(row.get("credentialRevision"), bool)
            or row["credentialRevision"] < 0
            or not isinstance(row.get("enabledFeatures"), list)
            or any(not isinstance(value, str) or not value for value in row["enabledFeatures"])
            or len(row["enabledFeatures"]) != len(set(row["enabledFeatures"]))
            or not isinstance(row.get("requiredOAuthScopes"), list)
            or any(not isinstance(value, str) or not value for value in row["requiredOAuthScopes"])
            or len(row["requiredOAuthScopes"]) != len(set(row["requiredOAuthScopes"]))
            or not isinstance(row.get("targetRefCount"), int)
            or isinstance(row.get("targetRefCount"), bool)
            or row["targetRefCount"] < 0
            or not isinstance(row.get("reauthRequired"), bool)
            or (
                row.get("credentialExpiresAt") is not None
                and (
                    not isinstance(row.get("credentialExpiresAt"), int)
                    or isinstance(row.get("credentialExpiresAt"), bool)
                    or row["credentialExpiresAt"] <= 0
                )
            )
            or (
                row.get("refreshExpiresAt") is not None
                and (
                    not isinstance(row.get("refreshExpiresAt"), int)
                    or isinstance(row.get("refreshExpiresAt"), bool)
                    or row["refreshExpiresAt"] <= 0
                )
            )
            or (
                row.get("reauthReasonCode") is not None
                and not isinstance(row.get("reauthReasonCode"), str)
            )
        ):
            raise StageTransitionError("Exchange Graph profile metadata is malformed")
        if "readOnly" in row and not isinstance(row["readOnly"], bool):
            raise StageTransitionError("Exchange readiness profile read-only marker is malformed")
        key = (provider, profile_ref)
        if key in profile_keys:
            raise StageTransitionError("Exchange readiness has duplicate provider profiles")
        profile_keys.add(key)

    for row in connections:
        if not isinstance(row, dict):
            raise StageTransitionError("Exchange readiness connection row is malformed")
        provider = row.get("providerId")
        if not isinstance(provider, str) or provider not in allowed_provider_ids:
            raise StageTransitionError("Exchange readiness connection contains unsupported fields")
        required = {
            "connectionRef",
            "providerId",
            "profileRef",
            "profileAlias",
            "accessMode",
            "readOnly",
        }
        if provider == "computer":
            # The official Exchange readiness DTO has neither targetAlias nor targetScope for
            # its fixed computer profile. Keep this provider's smaller shape closed.
            optional: set[str] = set()
        elif provider == "microsoft_graph":
            required.add("targetAlias")
            optional = set()
        else:
            required.update({"targetAlias", "targetScope"})
            optional = set()
        if (
            set(row) - required - optional
            or not required <= set(row)
            or not isinstance(row.get("connectionRef"), str)
            or not isinstance(row.get("profileRef"), str)
            or not isinstance(row.get("accessMode"), str)
            or not isinstance(row.get("readOnly"), bool)
            or not isinstance(row.get("profileAlias"), (str, type(None)))
            or (
                provider != "computer" and not isinstance(row.get("targetAlias"), (str, type(None)))
            )
            or (provider == "microsoft_graph" and row.get("targetAlias") is not None)
            or ("targetScope" in row and not isinstance(row["targetScope"], dict))
        ):
            raise StageTransitionError("Exchange readiness connection contains unsupported fields")


def _listener_repair_new_provider_ids(compose: dict[str, Any]) -> set[str]:
    """Return the closed HF/Azure/GCP set for this bootstrap-only listener repair.

    中文：listener repair 只接受当前已配置的 HF、Azure 与 GCP，不含 RunPod。
    """

    if not preflight._huggingface_enabled(compose):
        raise StageTransitionError("listener repair requires the selected Hugging Face provider")
    cloud_service = compose.get("services", {}).get("cloud-management-phase2", {})
    cloud_environment = (
        cloud_service.get("environment", {}) if isinstance(cloud_service, dict) else {}
    )
    try:
        cloud_document = json.loads(
            cloud_environment.get("CYRENE_CAPABILITY_CONFIGURATION_JSON", ""),
            object_pairs_hook=preflight._unique_json_object,
        )
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise StageTransitionError("candidate cloud provider scope is invalid") from error
    profiles = cloud_document.get("profiles") if isinstance(cloud_document, dict) else None
    if not isinstance(profiles, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("provider"), str) for row in profiles
    ):
        raise StageTransitionError("candidate cloud provider scope is malformed")
    provider_ids = {row["provider"] for row in profiles}
    if provider_ids != {"azure", "gcp"} or len(provider_ids) != len(profiles):
        raise StageTransitionError("listener repair requires exactly the issued Azure/GCP profiles")
    return {"huggingface", *provider_ids}


def _verify_listener_repair_connector_projection(
    *,
    snapshot: dict[str, Any],
    live_bindings: list[dict[str, Any]],
    live_catalog: dict[str, Any],
    candidate_bindings: list[dict[str, Any]],
    candidate_catalog: dict[str, Any],
    compose: dict[str, Any],
    include_live_catalog: bool,
) -> None:
    """Join the frozen 11 Navigator rows to exactly three already-issued read-only rows.

    中文：不合成 ID；HF、Azure、GCP 必须来自 actor-scoped Exchange 实际导出。
    """

    static_provider_ids = {row["providerId"] for row in candidate_bindings}
    expected_static_providers = preflight._expected_connector_providers(
        compose, "connector-auth-readonly", "provider-bootstrap"
    )
    expected_new_providers = _listener_repair_new_provider_ids(compose)
    if (
        len(candidate_bindings) != 11
        or static_provider_ids != expected_static_providers
        or static_provider_ids != {"github", "computer"}
        or sum(row["providerId"] == "github" for row in candidate_bindings) != 10
        or sum(row["providerId"] == "computer" for row in candidate_bindings) != 1
    ):
        raise StageTransitionError(
            "listener repair requires the exact existing 11-binding Navigator baseline"
        )
    expected_provider_ids = static_provider_ids | expected_new_providers
    live_provider_ids = {row["providerId"] for row in live_bindings}
    connections = snapshot.get("connections")
    profiles = snapshot.get("profiles")
    expected_connection_counts = {
        "azure": 1,
        "computer": 1,
        "gcp": 1,
        "github": 10,
        "huggingface": 1,
    }
    live_binding_counts = {
        provider: sum(row["providerId"] == provider for row in live_bindings)
        for provider in expected_provider_ids
    }
    live_connection_counts = (
        {
            provider: sum(
                isinstance(row, dict) and row.get("providerId") == provider for row in connections
            )
            for provider in expected_provider_ids
        }
        if isinstance(connections, list)
        else {}
    )
    live_profile_counts = (
        {
            provider: sum(
                isinstance(row, dict) and row.get("providerId") == provider for row in profiles
            )
            for provider in expected_provider_ids
        }
        if isinstance(profiles, list)
        else {}
    )
    if (
        len(live_bindings) != 14
        or live_provider_ids != expected_provider_ids
        or not isinstance(connections, list)
        or len(connections) != 14
        or not isinstance(profiles, list)
        or len(profiles) != 5
        or live_binding_counts != expected_connection_counts
        or live_connection_counts != expected_connection_counts
        or live_profile_counts != {provider: 1 for provider in expected_provider_ids}
    ):
        raise StageTransitionError(
            "listener repair requires exactly the existing 14 actor-scoped Exchange connections"
        )
    connection_refs = [row["connectionRef"] for row in connections]
    if len(connection_refs) != len(set(connection_refs)):
        raise StageTransitionError("Exchange issued connections contain duplicate connection IDs")

    live_static_bindings = [
        row for row in live_bindings if row["providerId"] in static_provider_ids
    ]
    live_new_bindings = [
        row for row in live_bindings if row["providerId"] in expected_new_providers
    ]
    if _canonical_json(live_static_bindings) != _canonical_json(candidate_bindings):
        raise StageTransitionError(
            "live Exchange 11-binding Navigator baseline differs from its frozen configuration"
        )
    preflight._validate_connector_bindings_document(
        {"schemaVersion": 1, "bindings": live_bindings}, expected_provider_ids
    )
    preflight._validate_new_provider_bindings_readonly(
        {"bindings": [row for row in live_new_bindings if row["providerId"] == "huggingface"]},
        {"huggingface"},
    )
    if not include_live_catalog:
        full_cloud_operations = {
            operation
            for operation in preflight.CONNECTOR_OPERATION_POLICIES
            if operation.startswith("cloud.") and operation != "cloud.resource.restart"
        }
        for binding in live_new_bindings:
            if binding["providerId"] not in {"azure", "gcp"}:
                continue
            observed_operations = {tool["operationId"] for tool in binding["tools"]}
            if observed_operations != full_cloud_operations:
                raise StageTransitionError(
                    "pre-repair Azure/GCP metadata must contain the complete canonical operation map"
                )
        if live_catalog["items"]:
            raise StageTransitionError(
                "pre-repair issued-state readback must not claim a live provider catalog"
            )
        return

    preflight._validate_new_provider_bindings_readonly(
        {"bindings": live_new_bindings}, expected_new_providers
    )

    live_catalog_document = {"schemaVersion": 1, "items": live_catalog["items"]}
    preflight._validate_connector_tool_catalog_documents(
        live_catalog_document,
        {"schemaVersion": 1, "bindings": live_bindings},
        expected_provider_ids,
    )
    static_binding_ids = {row["bindingId"] for row in candidate_bindings}
    live_static_catalog = {
        "schemaVersion": 1,
        "items": [row for row in live_catalog["items"] if row["bindingId"] in static_binding_ids],
    }
    if _canonical_json(live_static_catalog) != _canonical_json(candidate_catalog):
        raise StageTransitionError(
            "live Exchange catalog for the frozen 11 Navigator bindings changed"
        )


def _github_image_update_connection_refs(
    *,
    snapshot: dict[str, Any],
    compose: dict[str, Any],
    frozen_bindings: list[dict[str, Any]],
) -> list[str]:
    """Join all ten live GitHub connections to the frozen issued binding and repository scope."""

    bootstrap = _candidate_config_json(compose, "connector-bootstrap")
    github_bootstrap = bootstrap.get("github")
    expected_repositories = (
        github_bootstrap.get("repositories") if isinstance(github_bootstrap, dict) else None
    )
    if (
        not isinstance(github_bootstrap, dict)
        or github_bootstrap.get("expectedAccessMode") != "workspace_operator"
        or not isinstance(expected_repositories, list)
        or len(expected_repositories) != 10
    ):
        raise StageTransitionError(
            "GitHub image update requires the exact ten-repository workspace_operator bootstrap scope"
        )

    expected_by_id: dict[str, tuple[str, str]] = {}
    for row in expected_repositories:
        if (
            not isinstance(row, dict)
            or set(row) != {"repositoryId", "owner", "name"}
            or any(not isinstance(row.get(field), str) or not row[field] for field in row)
        ):
            raise StageTransitionError("GitHub bootstrap repository scope is malformed")
        repository_id = row["repositoryId"]
        if repository_id in expected_by_id:
            raise StageTransitionError("GitHub bootstrap repository scope has duplicate IDs")
        expected_by_id[repository_id] = (row["owner"], row["name"])

    profiles = [
        row
        for row in snapshot.get("profiles", [])
        if isinstance(row, dict) and row.get("providerId") == "github"
    ]
    connections = [
        row
        for row in snapshot.get("connections", [])
        if isinstance(row, dict) and row.get("providerId") == "github"
    ]
    bindings = [
        row
        for row in frozen_bindings
        if isinstance(row, dict) and row.get("providerId") == "github"
    ]
    if len(profiles) != 1 or len(connections) != 10 or len(bindings) != 10:
        raise StageTransitionError(
            "GitHub image update requires one live profile and exactly ten issued connections/bindings"
        )
    profile = profiles[0]
    profile_ref = profile.get("profileRef")
    profile_scope = profile.get("targetScope")
    live_repositories = (
        profile_scope.get("repositories") if isinstance(profile_scope, dict) else None
    )
    if (
        not isinstance(profile_ref, str)
        or not profile_ref
        or profile.get("accessMode") != github_bootstrap["expectedAccessMode"]
        or not isinstance(live_repositories, list)
        or len(live_repositories) != 10
    ):
        raise StageTransitionError("live GitHub profile scope/access mode is malformed")

    live_by_id: dict[str, tuple[str, str]] = {}
    for row in live_repositories:
        if (
            not isinstance(row, dict)
            or set(row) != {"repositoryId", "owner", "name"}
            or any(not isinstance(row.get(field), str) or not row[field] for field in row)
        ):
            raise StageTransitionError("live GitHub profile repository scope is malformed")
        repository_id = row["repositoryId"]
        if repository_id in live_by_id:
            raise StageTransitionError("live GitHub profile repository scope has duplicate IDs")
        live_by_id[repository_id] = (row["owner"], row["name"])
    if live_by_id != expected_by_id:
        raise StageTransitionError(
            "live GitHub profile repository scope differs from the frozen bootstrap config"
        )

    binding_refs: list[str] = []
    for row in bindings:
        connection_ref = row.get("connectionRef")
        try:
            canonical_ref = str(uuid.UUID(connection_ref))
        except (ValueError, TypeError, AttributeError) as error:
            raise StageTransitionError(
                "frozen GitHub binding has a non-canonical connection reference"
            ) from error
        if (
            canonical_ref != connection_ref
            or row.get("profileRef") != profile_ref
            or canonical_ref in binding_refs
        ):
            raise StageTransitionError(
                "frozen GitHub binding refs/profile do not match the live issued profile"
            )
        binding_refs.append(canonical_ref)

    connection_refs: list[str] = []
    scoped_repository_ids: list[str] = []
    for row in connections:
        connection_ref = row.get("connectionRef")
        target_scope = row.get("targetScope")
        try:
            canonical_ref = str(uuid.UUID(connection_ref))
        except (ValueError, TypeError, AttributeError) as error:
            raise StageTransitionError(
                "live GitHub connection has a non-canonical connection reference"
            ) from error
        if (
            canonical_ref != connection_ref
            or canonical_ref in connection_refs
            or row.get("profileRef") != profile_ref
            or row.get("accessMode") != github_bootstrap["expectedAccessMode"]
            or not isinstance(target_scope, dict)
            or set(target_scope) != {"repositoryId", "repository"}
        ):
            raise StageTransitionError(
                "live GitHub connection identity/access mode is malformed or duplicated"
            )
        repository_id = target_scope.get("repositoryId")
        repository = target_scope.get("repository")
        expected_repository = expected_by_id.get(repository_id)
        if (
            not isinstance(repository_id, str)
            or expected_repository is None
            or repository != f"{expected_repository[0]}/{expected_repository[1]}"
            or repository_id in scoped_repository_ids
        ):
            raise StageTransitionError(
                "live GitHub connection target differs from the frozen repository scope"
            )
        connection_refs.append(canonical_ref)
        scoped_repository_ids.append(repository_id)

    if set(connection_refs) != set(binding_refs) or set(scoped_repository_ids) != set(
        expected_by_id
    ):
        raise StageTransitionError(
            "live GitHub connection refs/scopes do not match the ten frozen issued bindings"
        )
    return sorted(connection_refs)


def _validate_unissued_account_center_graph_contract(
    *,
    compose: dict[str, Any],
    bindings_document: Any,
    catalog: dict[str, Any],
    phase: str,
) -> None:
    """Keep configured Graph scope and catalog checks while OAuth has no issued profile."""

    services = compose.get("services")
    graph_service = services.get("microsoft-graph") if isinstance(services, dict) else None
    graph_environment = (
        graph_service.get("environment") if isinstance(graph_service, dict) else None
    )
    encoded_graph_config = (
        graph_environment.get("CYRENE_CAPABILITY_CONFIGURATION_JSON")
        if isinstance(graph_environment, dict)
        else None
    )
    exchange_service = services.get("exchange") if isinstance(services, dict) else None
    exchange_environment = (
        exchange_service.get("environment") if isinstance(exchange_service, dict) else None
    )
    encoded_exchange_config = (
        exchange_environment.get("CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON")
        if isinstance(exchange_environment, dict)
        else None
    )
    try:
        graph_config = json.loads(
            encoded_graph_config, object_pairs_hook=preflight._unique_json_object
        )
        exchange_config = json.loads(
            encoded_exchange_config, object_pairs_hook=preflight._unique_json_object
        )
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise StageTransitionError(
            "unissued Microsoft Graph service/Exchange configuration is invalid"
        ) from error
    authorization_cloud = (
        exchange_config.get("authorizationCloud") if isinstance(exchange_config, dict) else None
    )
    configured_profiles = (
        authorization_cloud.get("profiles") if isinstance(authorization_cloud, dict) else None
    )
    graph_profiles = (
        [
            (alias, row)
            for alias, row in configured_profiles.items()
            if isinstance(row, dict) and row.get("provider") == "microsoft_graph"
        ]
        if isinstance(configured_profiles, dict)
        else []
    )
    if not isinstance(graph_config, dict) or len(graph_profiles) != 1:
        raise StageTransitionError(
            "unissued Microsoft Graph requires its exact configured service and host profile"
        )
    _graph_alias, graph_host_profile = graph_profiles[0]
    graph_office_scope = graph_host_profile.get("officeV2")
    graph_provider_scope = graph_config.get("officeV2")
    try:
        preflight._validate_office_v2_host_scope(graph_office_scope, compose)
        preflight._validate_graph_provider_office_flags(graph_provider_scope, graph_office_scope)
        graph_operations = preflight._graph_operations_for_office_scope(graph_office_scope)
    except (preflight.PreflightError, KeyError, TypeError, ValueError) as error:
        raise StageTransitionError(
            "unissued Microsoft Graph host/provider scope is invalid"
        ) from error
    if not graph_operations:
        raise StageTransitionError("unissued Microsoft Graph scope has no enabled Office feature")
    write_enabled = any(
        graph_office_scope["enabledFeatures"][feature]
        for feature in ("plannerEnabled", "todoEnabled", "calendarEnabled")
    )
    if graph_config.get("read_only") is not (not write_enabled):
        raise StageTransitionError(
            "unissued Microsoft Graph provider read-only mode differs from its Office scope"
        )

    candidate_bindings = (
        bindings_document.get("bindings") if isinstance(bindings_document, dict) else None
    )
    graph_bindings = (
        [
            row
            for row in candidate_bindings
            if isinstance(row, dict) and row.get("providerId") == "microsoft_graph"
        ]
        if isinstance(candidate_bindings, list)
        else []
    )
    catalog_items = catalog.get("items") if isinstance(catalog, dict) else None
    graph_catalog_items = (
        [
            row
            for row in catalog_items
            if isinstance(row, dict) and row.get("providerId") == "microsoft_graph"
        ]
        if isinstance(catalog_items, list)
        else []
    )
    if phase == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE:
        if graph_bindings or graph_catalog_items:
            raise StageTransitionError(
                "Graph bootstrap must keep unissued Graph bindings/catalog entries absent"
            )
        return
    if len(graph_bindings) != 1:
        raise StageTransitionError(
            "unissued Microsoft Graph requires its single frozen host binding"
        )
    graph_binding = graph_bindings[0]
    try:
        preflight._validate_new_provider_bindings_readonly(
            {"bindings": graph_bindings},
            {"microsoft_graph"},
            graph_office_scope=graph_office_scope,
        )
    except preflight.PreflightError as error:
        raise StageTransitionError(
            "unissued Microsoft Graph operation map differs from its configured Office scope"
        ) from error
    if len(graph_catalog_items) != 1:
        raise StageTransitionError(
            "unissued Microsoft Graph requires its single frozen tool catalog entry"
        )
    graph_catalog = graph_catalog_items[0]
    mapped_tools = {row["toolId"]: row for row in graph_binding["tools"]}
    catalog_tools = {row["id"]: row for row in graph_catalog["tools"]}
    if (
        graph_catalog.get("bindingId") != graph_binding.get("bindingId")
        or graph_catalog.get("connectionRef") != graph_binding.get("connectionRef")
        or graph_catalog.get("profileRef") != graph_binding.get("profileRef")
        or set(mapped_tools) != set(catalog_tools)
        or any(
            catalog_tools[tool_id].get("readOnly")
            is not (not preflight.CONNECTOR_OPERATION_POLICIES[row["operationId"]][2])
            for tool_id, row in mapped_tools.items()
        )
    ):
        raise StageTransitionError(
            "unissued Microsoft Graph tool catalog differs from its frozen operation map"
        )


def _account_center_expected_exchange_inventory(
    *,
    compose: dict[str, Any],
    phase: str,
    graph_unissued: bool,
    bindings_document: dict[str, Any],
    catalog_document: dict[str, Any],
) -> dict[str, Any]:
    """Derive the AC readiness inventory from the exact rendered connector inputs."""

    try:
        candidate_provider_ids = preflight._expected_connector_providers(
            compose, "connector-auth-readonly", phase
        )
        preflight._validate_connector_bindings_document(bindings_document, candidate_provider_ids)
        preflight._validate_connector_tool_catalog_documents(
            catalog_document,
            bindings_document,
            candidate_provider_ids,
        )
        if "computer" in candidate_provider_ids:
            preflight._validate_computer_exchange_profile(compose)
    except preflight.PreflightError as error:
        raise StageTransitionError(
            "Account Center Exchange inventory differs from its frozen provider contracts"
        ) from error

    candidate_bindings = bindings_document.get("bindings")
    if not isinstance(candidate_bindings, list):
        raise StageTransitionError("Account Center candidate bindings are malformed")
    binding_counts = {
        provider: sum(
            isinstance(row, dict) and row.get("providerId") == provider
            for row in candidate_bindings
        )
        for provider in candidate_provider_ids
    }
    expected_binding_counts = {
        provider: 10 if provider == "github" else 1 for provider in candidate_provider_ids
    }
    if binding_counts != expected_binding_counts:
        raise StageTransitionError(
            "Account Center candidate binding counts differ from the frozen provider inventory"
        )

    active_provider_ids = set(candidate_provider_ids)
    if graph_unissued:
        active_provider_ids.discard("microsoft_graph")
    expected_profile_counts = {provider: 1 for provider in active_provider_ids}
    expected_connection_counts = {
        provider: 10 if provider == "github" else 1 for provider in active_provider_ids
    }
    expected_binding_count = sum(binding_counts[provider] for provider in active_provider_ids)
    return {
        "candidateProviderIds": candidate_provider_ids,
        "activeProviderIds": active_provider_ids,
        "expectedProfileCounts": expected_profile_counts,
        "expectedConnectionCounts": expected_connection_counts,
        "expectedBindingCount": expected_binding_count,
        "expectedProfileCount": len(expected_profile_counts),
        "expectedConnectionCount": sum(expected_connection_counts.values()),
    }


def _verify_computer_exchange_readiness(
    *,
    compose: dict[str, Any],
    profile: dict[str, Any],
    connection: dict[str, Any],
    binding: dict[str, Any],
    catalog_item: dict[str, Any] | None,
    include_live_catalog: bool,
) -> None:
    """Join Computer's live profile, connection and four canonical tools to the host config."""

    try:
        preflight._validate_computer_exchange_profile(compose)
    except preflight.PreflightError as error:
        raise StageTransitionError(
            "candidate Computer host profile does not match its private mTLS workspace contract"
        ) from error
    exchange_service = compose.get("services", {}).get("exchange", {})
    environment = (
        exchange_service.get("environment", {}) if isinstance(exchange_service, dict) else {}
    )
    try:
        integration = json.loads(
            environment.get("CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON", ""),
            object_pairs_hook=preflight._unique_json_object,
        )
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise StageTransitionError(
            "candidate Computer host integration config is invalid"
        ) from error
    connector_host = integration.get("connectorHost") if isinstance(integration, dict) else None
    runner = connector_host.get("computerRunner") if isinstance(connector_host, dict) else None
    if not isinstance(runner, dict):
        raise StageTransitionError("candidate Computer host profile is unavailable")

    expected_scope = {
        "maxCommandTimeoutMs": runner["maxCommandTimeoutMs"],
        "maxDirectoryDepth": runner["maxDirectoryDepth"],
        "maxFileBytes": runner["maxFileBytes"],
        "maxReadBytes": runner["maxReadBytes"],
    }
    expected_operations = {
        "computer.command.execute",
        "computer.directory.list",
        "computer.file.read",
        "computer.file.write",
    }
    operation_ids = {
        tool.get("operationId") for tool in binding.get("tools", []) if isinstance(tool, dict)
    }
    try:
        preflight._validate_connector_bindings_document(
            {"schemaVersion": 1, "bindings": [binding]}, {"computer"}
        )
        if include_live_catalog:
            if not isinstance(catalog_item, dict):
                raise StageTransitionError("live Computer tool catalog entry is missing")
            preflight._validate_connector_tool_catalog_documents(
                {"schemaVersion": 1, "items": [catalog_item]},
                {"schemaVersion": 1, "bindings": [binding]},
                {"computer"},
            )
    except preflight.PreflightError as error:
        raise StageTransitionError(
            "Computer binding or tool catalog differs from the canonical operation map"
        ) from error

    if (
        operation_ids != expected_operations
        or profile.get("profileRef") != runner.get("profileRef")
        or profile.get("profileAlias") != runner.get("profileRef")
        or profile.get("accessMode") != "workspace_operator"
        or profile.get("targetScope") != expected_scope
        or connection.get("connectionRef") != runner.get("connectionRef")
        or connection.get("connectionRef") != binding.get("connectionRef")
        or connection.get("profileRef") != runner.get("profileRef")
        or connection.get("profileAlias") != runner.get("profileRef")
        or connection.get("accessMode") != "workspace_operator"
        or connection.get("readOnly") is not False
        or binding.get("bindingId") != f"exchange-computer-{runner.get('connectionRef')}"
        or binding.get("profileRef") != runner.get("profileRef")
        or (
            include_live_catalog
            and (
                catalog_item.get("bindingId") != binding.get("bindingId")
                or catalog_item.get("connectionRef") != binding.get("connectionRef")
                or catalog_item.get("profileRef") != binding.get("profileRef")
                or {
                    tool.get("id")
                    for tool in catalog_item.get("tools", [])
                    if isinstance(tool, dict)
                }
                != expected_operations
            )
        )
    ):
        raise StageTransitionError(
            "live Computer identity, workspace scope, connection mode or catalog does not join to its host profile"
        )


def _verify_exchange_readiness_snapshot(
    *,
    snapshot: Any,
    compose: dict[str, Any],
    phase: str = "issued-connector-activation",
    image_update_target: str | None = None,
    listener_repair: bool = False,
    include_live_catalog: bool = True,
    account_center_database_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify frozen tools and actor-scoped provider scopes for the selected transition.

    中文：listener repair 修复前只核已签发状态，双服务健康后再核完整实时目录。
    """

    if phase not in PHASES:
        raise StageTransitionError("Exchange readiness phase is unsupported")
    if (
        not isinstance(snapshot, dict)
        or set(snapshot)
        != {"schemaVersion", "actor", "profiles", "connections", "bindings", "catalog"}
        or snapshot.get("schemaVersion") != 1
        or isinstance(snapshot.get("schemaVersion"), bool)
    ):
        raise StageTransitionError("pinned Exchange returned an unsupported readiness schema")
    actor = preflight._connector_bootstrap_actor(compose)
    if (
        not isinstance(snapshot.get("actor"), dict)
        or set(snapshot["actor"]) != {"issuer", "subject", "organizationId", "workspaceId"}
        or snapshot["actor"] != actor
        or any(not isinstance(value, str) or not value for value in actor.values())
    ):
        raise StageTransitionError(
            "pinned Exchange readiness actor differs from candidate owner scope"
        )
    _assert_exchange_readiness_safe_shape(snapshot)

    account_center_graph_unissued = False
    if image_update_target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET:
        if phase not in ACCOUNT_CENTER_IMAGE_UPDATE_PHASES or not isinstance(
            account_center_database_state, dict
        ):
            raise StageTransitionError(
                "Account Center readiness requires its sealed database identity baseline"
            )
        _validate_account_center_database_state(account_center_database_state)
        account_center_graph_unissued = (
            account_center_database_state["additionalMicrosoftProfileCount"] == 0
        )
    elif account_center_database_state is not None:
        raise StageTransitionError(
            "Account Center database baseline is valid only for its issued image update"
        )

    bindings_document = _candidate_config_json(compose, "connector-bindings")
    catalog_document = _candidate_config_json(compose, "connector-tool-catalog")
    if (
        set(bindings_document) != {"schemaVersion", "bindings"}
        or bindings_document.get("schemaVersion") != 1
        or isinstance(bindings_document.get("schemaVersion"), bool)
    ):
        raise StageTransitionError("candidate connector binding projection is malformed")
    live_bindings = _sorted_exchange_bindings(snapshot.get("bindings"))
    candidate_bindings = _sorted_exchange_bindings(bindings_document.get("bindings"))
    live_catalog = _sorted_exchange_catalog(snapshot.get("catalog"))
    candidate_catalog = _sorted_exchange_catalog(catalog_document)
    graph_bootstrap_image_update = (
        not listener_repair
        and image_update_target
        in {WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET, EXCHANGE_IMAGE_UPDATE_TARGET}
        and phase == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
    )
    graph_bootstrap_image_update_label = (
        "Workspace Authority"
        if image_update_target == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET
        else "Exchange"
    )
    if listener_repair:
        _verify_listener_repair_connector_projection(
            snapshot=snapshot,
            live_bindings=live_bindings,
            live_catalog=live_catalog,
            candidate_bindings=candidate_bindings,
            candidate_catalog=candidate_catalog,
            compose=compose,
            include_live_catalog=include_live_catalog,
        )
    elif graph_bootstrap_image_update:
        frozen_graph_bindings = [
            row for row in candidate_bindings if row["providerId"] == "microsoft_graph"
        ]
        frozen_graph_catalog = [
            row for row in candidate_catalog["items"] if row["providerId"] == "microsoft_graph"
        ]
        live_graph_bindings = [
            row for row in live_bindings if row["providerId"] == "microsoft_graph"
        ]
        live_graph_catalog = [
            row for row in live_catalog["items"] if row["providerId"] == "microsoft_graph"
        ]
        if frozen_graph_bindings or frozen_graph_catalog:
            raise StageTransitionError(
                f"{graph_bootstrap_image_update_label} Graph-bootstrap inputs must keep Graph bindings/catalog absent"
            )
        if len(live_graph_bindings) != 1 or len(live_graph_catalog) != 1:
            raise StageTransitionError(
                f"{graph_bootstrap_image_update_label} Graph-bootstrap readiness requires one live Graph binding and catalog entry"
            )
        frozen_non_graph_bindings = [
            row for row in candidate_bindings if row["providerId"] != "microsoft_graph"
        ]
        live_non_graph_bindings = [
            row for row in live_bindings if row["providerId"] != "microsoft_graph"
        ]
        frozen_non_graph_catalog = [
            row for row in candidate_catalog["items"] if row["providerId"] != "microsoft_graph"
        ]
        live_non_graph_catalog = [
            row for row in live_catalog["items"] if row["providerId"] != "microsoft_graph"
        ]
        if _canonical_json(live_non_graph_bindings) != _canonical_json(frozen_non_graph_bindings):
            raise StageTransitionError(
                "live Exchange non-Graph bindings differ from the frozen Navigator binding config"
            )
        if _canonical_json(live_non_graph_catalog) != _canonical_json(frozen_non_graph_catalog):
            raise StageTransitionError(
                "live non-Graph tool catalog differs from the frozen Navigator catalog config"
            )
        graph_binding = live_graph_bindings[0]
        graph_catalog_item = live_graph_catalog[0]
        if (
            graph_catalog_item["bindingId"] != graph_binding["bindingId"]
            or graph_catalog_item["connectionRef"] != graph_binding["connectionRef"]
            or graph_catalog_item["profileRef"] != graph_binding["profileRef"]
        ):
            raise StageTransitionError(
                "live Graph catalog entry does not join to its single binding"
            )
    else:
        if _canonical_json(live_bindings) != _canonical_json(candidate_bindings):
            raise StageTransitionError(
                "live Exchange issued bindings differ from the frozen Navigator binding config"
            )
        if _canonical_json(live_catalog) != _canonical_json(candidate_catalog):
            raise StageTransitionError(
                "live provider tool catalog differs from the frozen Navigator catalog config"
            )

    if (
        not listener_repair
        and phase == "provider-bootstrap"
        and image_update_target in EXCHANGE_READINESS_IMAGE_TARGETS
    ):
        provider_ids = sorted({row["providerId"] for row in live_bindings})
        if not provider_ids:
            raise StageTransitionError("provider-bootstrap Exchange binding inventory is empty")
        return {
            "schemaVersion": 1,
            "status": "LIVE_VERIFIED",
            "actorSha256": _sha256_bytes(_canonical_json(actor)),
            "bindingsSha256": _sha256_bytes(_canonical_json(live_bindings)),
            "catalogSha256": _sha256_bytes(_canonical_json(live_catalog)),
            "profileCount": len(snapshot["profiles"]),
            "connectionCount": len(snapshot["connections"]),
            "bindingCount": len(live_bindings),
            "verifiedProviderIds": provider_ids,
            "profileRefByProvider": {},
        }

    if not listener_repair and phase == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE:
        provider_ids = {row["providerId"] for row in live_bindings}
        if image_update_target not in {
            WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET,
            EXCHANGE_IMAGE_UPDATE_TARGET,
        } and (
            "microsoft_graph" in provider_ids
            or any(row.get("providerId") == "microsoft_graph" for row in snapshot["profiles"])
            or any(row.get("providerId") == "microsoft_graph" for row in snapshot["connections"])
        ):
            raise StageTransitionError(
                "Graph bootstrap must not claim an issued Office profile before OAuth completes"
            )
        if image_update_target not in {
            ACCOUNT_CENTER_IMAGE_UPDATE_TARGET,
            WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET,
            EXCHANGE_IMAGE_UPDATE_TARGET,
        }:
            return {
                "schemaVersion": 1,
                "status": "LIVE_VERIFIED",
                "actorSha256": _sha256_bytes(_canonical_json(actor)),
                "bindingsSha256": _sha256_bytes(_canonical_json(live_bindings)),
                "catalogSha256": _sha256_bytes(_canonical_json(live_catalog)),
                "profileCount": len(snapshot["profiles"]),
                "connectionCount": len(snapshot["connections"]),
                "bindingCount": len(live_bindings),
                "verifiedProviderIds": sorted(provider_ids),
                "profileRefByProvider": {},
            }

    cloud_service = compose.get("services", {}).get("cloud-management-phase2", {})
    cloud_env = cloud_service.get("environment", {}) if isinstance(cloud_service, dict) else {}
    try:
        cloud_document = json.loads(
            cloud_env.get("CYRENE_CAPABILITY_CONFIGURATION_JSON", ""),
            object_pairs_hook=preflight._unique_json_object,
        )
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise StageTransitionError("candidate cloud provider scope is invalid") from error
    cloud_profiles = cloud_document.get("profiles") if isinstance(cloud_document, dict) else None
    if not isinstance(cloud_profiles, list):
        raise StageTransitionError("candidate cloud provider scope is malformed")
    if any(
        not isinstance(row, dict) or not isinstance(row.get("provider"), str)
        for row in cloud_profiles
    ):
        raise StageTransitionError("candidate cloud provider rows are malformed")
    cloud_profile_by_provider = {row["provider"]: row for row in cloud_profiles}
    cloud_provider_ids = set(cloud_profile_by_provider)
    if len(cloud_provider_ids) != len(cloud_profiles) or cloud_provider_ids not in (
        {"azure", "gcp"},
        {"azure", "gcp", "runpod"},
    ):
        raise StageTransitionError("candidate cloud provider set is unsupported")
    if any(
        profile.get("read_only") is not True
        or not isinstance(profile.get("profile_ref"), str)
        or not profile["profile_ref"]
        or not isinstance(profile.get("identity_ref"), str)
        or not profile["identity_ref"]
        for profile in cloud_profile_by_provider.values()
    ):
        raise StageTransitionError(
            "candidate cloud providers require exact profile/identity refs and server-enforced read_only"
        )

    graph_service_configured = "microsoft-graph" in compose.get("services", {})
    account_center_inventory: dict[str, Any] | None = None
    if image_update_target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET:
        account_center_inventory = _account_center_expected_exchange_inventory(
            compose=compose,
            phase=phase,
            graph_unissued=account_center_graph_unissued,
            bindings_document=bindings_document,
            catalog_document=catalog_document,
        )
        expected_provider_ids = set(account_center_inventory["activeProviderIds"]) - {"github"}
    else:
        expected_provider_ids = {"huggingface", *cloud_provider_ids}
        if graph_service_configured and not account_center_graph_unissued:
            expected_provider_ids.add("microsoft_graph")
    bindings_by_provider: dict[str, dict[str, Any]] = {}
    provider_binding_rows = (
        live_bindings
        if listener_repair or graph_bootstrap_image_update
        else bindings_document["bindings"]
    )
    for row in provider_binding_rows:
        if isinstance(row, dict) and row.get("providerId") in expected_provider_ids:
            provider_id = row["providerId"]
            if provider_id in bindings_by_provider:
                raise StageTransitionError(
                    f"candidate has multiple Exchange bindings for {provider_id}"
                )
            bindings_by_provider[provider_id] = row
    if set(bindings_by_provider) != expected_provider_ids:
        raise StageTransitionError("candidate issued binding set differs from configured providers")

    profiles_by_provider: dict[str, list[dict[str, Any]]] = {}
    for row in snapshot["profiles"]:
        if row.get("providerId") in expected_provider_ids:
            profiles_by_provider.setdefault(row["providerId"], []).append(row)
    connections_by_provider: dict[str, list[dict[str, Any]]] = {}
    for row in snapshot["connections"]:
        if row.get("providerId") in expected_provider_ids:
            connections_by_provider.setdefault(row["providerId"], []).append(row)

    if account_center_inventory is not None:
        observed_profile_counts: dict[str, int] = {}
        for row in snapshot["profiles"]:
            provider = row["providerId"]
            observed_profile_counts[provider] = observed_profile_counts.get(provider, 0) + 1
        observed_connection_counts: dict[str, int] = {}
        for row in snapshot["connections"]:
            provider = row["providerId"]
            observed_connection_counts[provider] = observed_connection_counts.get(provider, 0) + 1
        if (
            observed_profile_counts != account_center_inventory["expectedProfileCounts"]
            or observed_connection_counts != account_center_inventory["expectedConnectionCounts"]
        ):
            raise StageTransitionError(
                "live Account Center Exchange profiles/connections differ from the frozen provider inventory"
            )

    catalog_by_binding = {row["bindingId"]: row for row in live_catalog["items"]}
    exchange_service = compose.get("services", {}).get("exchange", {})
    exchange_environment = (
        exchange_service.get("environment", {}) if isinstance(exchange_service, dict) else {}
    )
    try:
        exchange_config = json.loads(
            exchange_environment.get("CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON", ""),
            object_pairs_hook=preflight._unique_json_object,
        )
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise StageTransitionError(
            "candidate Exchange provider profile config is invalid"
        ) from error
    if not isinstance(exchange_config, dict):
        raise StageTransitionError("candidate Exchange provider profile config is malformed")
    authorization_cloud = exchange_config.get("authorizationCloud")
    configured_profiles = (
        authorization_cloud.get("profiles") if isinstance(authorization_cloud, dict) else None
    )
    if not isinstance(configured_profiles, dict):
        raise StageTransitionError("candidate Exchange provider profiles are unavailable")

    graph_connection_status: str | None = None
    if account_center_graph_unissued:
        graph_profiles = [
            row for row in snapshot["profiles"] if row.get("providerId") == "microsoft_graph"
        ]
        graph_connections = [
            row for row in snapshot["connections"] if row.get("providerId") == "microsoft_graph"
        ]
        if graph_profiles or graph_connections:
            raise StageTransitionError(
                "Microsoft Graph profile or connection appeared after the sealed empty Account Center baseline"
            )
        if graph_service_configured:
            _validate_unissued_account_center_graph_contract(
                compose=compose,
                bindings_document=bindings_document,
                catalog=live_catalog,
                phase=phase,
            )
        graph_connection_status = "NOT_CONNECTED_NOT_VERIFIED"
    elif (
        image_update_target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
        and account_center_database_state["additionalMicrosoftProfileCount"] == 1
        and not graph_service_configured
    ):
        raise StageTransitionError(
            "sealed Account Center Graph profile has no configured Microsoft Graph service"
        )

    bootstrap = _candidate_config_json(compose, "connector-bootstrap")
    hf_bootstrap = bootstrap.get("huggingface")
    hf_alias = hf_bootstrap.get("profileAlias") if isinstance(hf_bootstrap, dict) else None
    if not isinstance(hf_alias, str) or not hf_alias:
        raise StageTransitionError("candidate HF profile alias is unavailable")
    hf_profile_config = configured_profiles.get(hf_alias)
    if not isinstance(hf_profile_config, dict) or hf_profile_config.get("provider") != "hf":
        raise StageTransitionError("candidate Exchange HF profile alias does not resolve to HF")
    hf_account_scopes = hf_profile_config.get("accountScopes")
    hf_repositories = preflight._normalize_exchange_huggingface_repository_scope(
        hf_account_scopes.get("repositories") if isinstance(hf_account_scopes, dict) else None,
        "candidate Exchange HF profile",
    )
    expected_account_scope = [
        {
            "repoType": row["repo_type"],
            "repoId": row["repo_id"],
            "permissions": row["permissions"],
        }
        for row in hf_repositories
    ]
    hf_seed_path = preflight._hf_seed_path(compose)
    try:
        hf_seed = json.loads(
            hf_seed_path.read_text(encoding="utf-8"),
            object_pairs_hook=preflight._unique_json_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise StageTransitionError(
            "candidate HF seed is unavailable for live scope comparison"
        ) from error
    if not isinstance(hf_seed, dict):
        raise StageTransitionError("candidate HF seed scope is malformed")

    live_profile_refs: dict[str, str] = {}
    live_connection_refs: dict[str, str] = {}
    for provider_id in sorted(expected_provider_ids):
        provider_profiles = profiles_by_provider.get(provider_id, [])
        provider_connections = connections_by_provider.get(provider_id, [])
        if len(provider_profiles) != 1 or len(provider_connections) != 1:
            raise StageTransitionError(
                f"live Exchange state must have exactly one profile and connection for {provider_id}"
            )
        profile = provider_profiles[0]
        connection = provider_connections[0]
        is_graph = provider_id == "microsoft_graph"
        is_computer = provider_id == "computer"
        if (
            not isinstance(profile.get("profileAlias"), str)
            or (
                is_graph
                and (connection.get("targetAlias") is not None or "targetScope" in connection)
            )
            or (
                not is_graph
                and not is_computer
                and (
                    not isinstance(connection.get("targetAlias"), str)
                    or not isinstance(connection.get("targetScope"), dict)
                )
            )
        ):
            raise StageTransitionError(
                f"live Exchange target/profile metadata is incomplete for {provider_id}"
            )
        binding = bindings_by_provider[provider_id]
        catalog_item = catalog_by_binding.get(binding["bindingId"])
        graph_office_scope: dict[str, Any] | None = None
        graph_operations: set[str] = set()
        graph_read_only = True
        if is_graph:
            graph_profiles = [
                (alias, row)
                for alias, row in configured_profiles.items()
                if isinstance(row, dict) and row.get("provider") == "microsoft_graph"
            ]
            if len(graph_profiles) != 1:
                raise StageTransitionError(
                    "candidate Exchange must have exactly one Microsoft Graph host profile"
                )
            graph_alias, graph_host_profile = graph_profiles[0]
            graph_office_scope = graph_host_profile.get("officeV2")
            if not isinstance(graph_office_scope, dict):
                raise StageTransitionError(
                    "candidate Exchange Graph Office v2 scope is unavailable"
                )
            try:
                graph_binding_document = {"schemaVersion": 1, "bindings": [binding]}
                preflight._validate_connector_bindings_document(
                    graph_binding_document, {"microsoft_graph"}
                )
                if include_live_catalog:
                    preflight._validate_connector_tool_catalog_documents(
                        {"schemaVersion": 1, "items": [catalog_item]},
                        graph_binding_document,
                        {"microsoft_graph"},
                    )
                preflight._validate_office_v2_host_scope(graph_office_scope, compose)
                graph_operations = preflight._graph_operations_for_office_scope(graph_office_scope)
                preflight._validate_new_provider_bindings_readonly(
                    {"bindings": [binding]},
                    {"microsoft_graph"},
                    graph_office_scope=graph_office_scope,
                )
            except preflight.PreflightError as error:
                raise StageTransitionError(
                    "Graph issued operation map differs from its Office v2 target scope"
                ) from error
            if not graph_operations:
                raise StageTransitionError(
                    "Graph activation must enable at least one Office feature"
                )
            graph_read_only = not any(
                preflight.CONNECTOR_OPERATION_POLICIES[operation][2]
                for operation in graph_operations
            )
            if graph_alias != profile.get("profileAlias"):
                raise StageTransitionError("live Graph profile alias differs from its host binding")
            if include_live_catalog and isinstance(catalog_item, dict):
                mapped_tools = {row["toolId"]: row for row in binding["tools"]}
                catalog_tools = {row["id"]: row for row in catalog_item["tools"]}
                if set(mapped_tools) != set(catalog_tools) or any(
                    catalog_tools[tool_id].get("readOnly")
                    is not (
                        not preflight.CONNECTOR_OPERATION_POLICIES[
                            mapped_tools[tool_id]["operationId"]
                        ][2]
                    )
                    for tool_id in mapped_tools
                ):
                    raise StageTransitionError(
                        "live Graph catalog mutability differs from its issued operation map"
                    )
        elif is_computer:
            _verify_computer_exchange_readiness(
                compose=compose,
                profile=profile,
                connection=connection,
                binding=binding,
                catalog_item=catalog_item,
                include_live_catalog=include_live_catalog,
            )
        expected_connection_read_only = graph_read_only if is_graph else not is_computer
        if (
            (include_live_catalog and not isinstance(catalog_item, dict))
            or connection["connectionRef"] != binding["connectionRef"]
            or connection["profileRef"] != binding["profileRef"]
            or profile["profileRef"] != binding["profileRef"]
            or connection["profileAlias"] != profile["profileAlias"]
            or connection["accessMode"] != profile["accessMode"]
            or (
                include_live_catalog and connection["readOnly"] is not expected_connection_read_only
            )
            or (
                include_live_catalog
                and not is_graph
                and not is_computer
                and (
                    not catalog_item["tools"]
                    or any(tool.get("readOnly") is not True for tool in catalog_item["tools"])
                )
            )
        ):
            raise StageTransitionError(
                f"live Exchange profile/connection access mode, binding, and read-only catalog do not join for {provider_id}"
            )
        live_profile_refs[provider_id] = profile["profileRef"]
        live_connection_refs[provider_id] = connection["connectionRef"]

        if provider_id == "huggingface":
            try:
                live_hf_repositories = preflight._normalize_exchange_huggingface_repository_scope(
                    profile["targetScope"].get("repositories"),
                    "live Account Center HF profile",
                )
            except preflight.PreflightError as error:
                raise StageTransitionError(
                    "live Account Center HF profile repository scope is invalid"
                ) from error
            if (
                profile["profileAlias"] != hf_alias
                or live_hf_repositories != hf_repositories
                or profile.get("authMode")
                != (hf_profile_config.get("authMode") or hf_seed.get("auth_mode"))
                or profile["accessMode"] != hf_bootstrap.get("expectedAccessMode")
            ):
                raise StageTransitionError(
                    "live Account Center HF profile scope/access mode differs from candidate config"
                )
            target_rows = hf_profile_config.get("targets")
            target_alias = connection.get("targetAlias")
            target = target_rows.get(target_alias) if isinstance(target_rows, dict) else None
            if not isinstance(target, dict) or connection["targetScope"] != next(
                (row for row in expected_account_scope if row["repoId"] == target.get("targetRef")),
                None,
            ):
                raise StageTransitionError(
                    "live HF connector target does not match the Account Center repository scope"
                )
            target_repo_type = connection["targetScope"].get("repoType")
            expected_list_operation = {
                "model": "huggingface.models.list",
                "dataset": "huggingface.datasets.list",
                "space": "huggingface.spaces.list",
            }.get(target_repo_type)
            observed_list_operations = {
                tool["operationId"]
                for tool in binding["tools"]
                if tool["operationId"]
                in {
                    "huggingface.models.list",
                    "huggingface.datasets.list",
                    "huggingface.spaces.list",
                }
            }
            if listener_repair and (
                expected_list_operation is None
                or observed_list_operations != {expected_list_operation}
            ):
                raise StageTransitionError(
                    "live HF list operation does not match the issued repository type"
                )
        elif provider_id == "microsoft_graph":
            tenant_id = graph_office_scope["tenantId"]
            expected_external_subject = f"{tenant_id}:{actor['subject']}"
            expected_scope = preflight._graph_public_target_scope(graph_office_scope)
            expected_features = preflight._graph_enabled_feature_names(graph_office_scope)
            expected_oauth_scopes = preflight._graph_expected_oauth_scopes(graph_office_scope)
            if (
                profile["profileAlias"] != graph_alias
                or profile.get("authStatus") != "healthy"
                or profile.get("authMode") != "delegated_oauth"
                or profile.get("accessMode") != "actor_owned"
                or profile.get("externalSubject") != expected_external_subject
                or profile.get("scopeSchemaVersion") != 2
                or profile.get("targetScope") != expected_scope
                or set(profile.get("enabledFeatures", [])) != expected_features
                or set(profile.get("requiredOAuthScopes", [])) != expected_oauth_scopes
                or profile.get("targetRefCount")
                != preflight._graph_target_ref_count(graph_office_scope)
                or profile.get("reauthRequired") is not False
                or profile.get("reauthReasonCode") is not None
                or not isinstance(profile.get("credentialExpiresAt"), int)
                or isinstance(profile.get("credentialExpiresAt"), bool)
                or profile.get("credentialExpiresAt") <= 0
                or (
                    profile.get("refreshExpiresAt") is not None
                    and (
                        not isinstance(profile.get("refreshExpiresAt"), int)
                        or isinstance(profile.get("refreshExpiresAt"), bool)
                        or profile.get("refreshExpiresAt") <= 0
                    )
                )
                or connection.get("accessMode") != "actor_owned"
                or connection.get("targetAlias") is not None
                or connection.get("readOnly") is not graph_read_only
            ):
                raise StageTransitionError(
                    "live Exchange Graph identity, credential health, or v2 target scope differs from the candidate"
                )
        elif provider_id == "computer":
            # The fixed host-owned workspace profile was joined above; it is not a Cloud profile.
            pass
        else:
            candidate_profiles = [
                (alias, row)
                for alias, row in configured_profiles.items()
                if isinstance(row, dict) and row.get("provider") == provider_id
            ]
            if len(candidate_profiles) != 1:
                raise StageTransitionError(
                    f"candidate Exchange must have exactly one host profile for {provider_id}"
                )
            alias, candidate_profile = candidate_profiles[0]
            plugin_profile = cloud_profile_by_provider[provider_id]
            if (
                alias != plugin_profile["profile_ref"]
                or candidate_profile.get("identityRef") != plugin_profile["identity_ref"]
                or profile["profileRef"] != alias
                or connection["profileRef"] != alias
                or binding["profileRef"] != alias
            ):
                raise StageTransitionError(
                    f"candidate Exchange profile identity does not join to Cloud Management for {provider_id}"
                )
            targets = candidate_profile.get("targets")
            expected_targets = (
                {
                    target_alias: {"targetRef": target.get("targetRef")}
                    for target_alias, target in targets.items()
                    if isinstance(target, dict)
                }
                if isinstance(targets, dict)
                else {}
            )
            expected_access = candidate_profile.get("accessMode", "actor_owned")
            target_alias = connection.get("targetAlias")
            candidate_target = targets.get(target_alias) if isinstance(targets, dict) else None
            if (
                profile["profileAlias"] != alias
                or profile["accessMode"] != expected_access
                or profile["targetScope"] != {"targets": expected_targets}
                or (include_live_catalog and profile.get("readOnly") is not True)
                or candidate_target is None
                or connection.get("targetScope")
                != {"targetAlias": target_alias, "targetRef": candidate_target.get("targetRef")}
            ):
                raise StageTransitionError(
                    f"live Exchange cloud profile scope/read-only state differs for {provider_id}"
                )

    github_connection_refs = (
        _github_image_update_connection_refs(
            snapshot=snapshot,
            compose=compose,
            frozen_bindings=candidate_bindings,
        )
        if image_update_target
        in {GITHUB_ISSUES_IMAGE_UPDATE_TARGET, ACCOUNT_CENTER_IMAGE_UPDATE_TARGET}
        and (
            phase == "issued-connector-activation"
            or (
                image_update_target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
                and phase == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
            )
        )
        else None
    )
    actor_digest = _sha256_bytes(_canonical_json(actor))
    bindings_digest = _sha256_bytes(
        _canonical_json(_sorted_exchange_bindings(snapshot["bindings"]))
    )
    catalog_digest = _sha256_bytes(_canonical_json(live_catalog)) if include_live_catalog else None
    verified_provider_ids = set(expected_provider_ids)
    if image_update_target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET:
        verified_provider_ids.add("github")
    result = {
        "schemaVersion": 1,
        "status": "LIVE_VERIFIED" if include_live_catalog else "ISSUED_STATE_VERIFIED",
        "actorSha256": actor_digest,
        "bindingsSha256": bindings_digest,
        "catalogSha256": catalog_digest,
        "catalogStatus": "LIVE_VERIFIED" if include_live_catalog else "NOT_LIVE_CATALOG",
        "profileCount": len(snapshot["profiles"]),
        "connectionCount": len(snapshot["connections"]),
        "bindingCount": len(snapshot["bindings"])
        - int(
            account_center_graph_unissued
            and graph_service_configured
            and phase != OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
        ),
        "verifiedProviderIds": sorted(
            {row["providerId"] for row in live_bindings}
            if listener_repair
            else verified_provider_ids
        ),
        "profileRefByProvider": live_profile_refs,
        **(
            {"huggingfaceConnectionRef": live_connection_refs[HUGGINGFACE_IMAGE_UPDATE_TARGET]}
            if image_update_target == HUGGINGFACE_IMAGE_UPDATE_TARGET
            else {}
        ),
        **(
            {
                "connectionRefByProvider": {
                    provider: live_connection_refs[provider]
                    for provider in sorted(expected_provider_ids & CLOUD_MONITOR_PROVIDER_IDS)
                }
            }
            if image_update_target == CLOUD_IMAGE_UPDATE_TARGET
            and phase == "issued-connector-activation"
            else {}
        ),
        **(
            {
                "connectionRefByProvider": {
                    "github": github_connection_refs,
                    **{
                        provider: connection_ref
                        for provider, connection_ref in live_connection_refs.items()
                        if provider not in {"computer", "microsoft_graph"}
                    },
                }
            }
            if image_update_target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
            and phase in ACCOUNT_CENTER_IMAGE_UPDATE_PHASES
            else {}
        ),
        **(
            {"connectionRefByProvider": dict(sorted(live_connection_refs.items()))}
            if image_update_target == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET
            and phase in WORKSPACE_AUTHORITY_IMAGE_UPDATE_PHASES
            else {}
        ),
        **(
            {"connectionRefByProvider": dict(sorted(live_connection_refs.items()))}
            if image_update_target == EXCHANGE_IMAGE_UPDATE_TARGET
            and phase == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
            else {}
        ),
        **(
            {"githubConnectionRefs": github_connection_refs}
            if image_update_target == GITHUB_ISSUES_IMAGE_UPDATE_TARGET
            and github_connection_refs is not None
            else {}
        ),
        **(
            {"issuedStateSha256": _exchange_issued_metadata_sha256(snapshot)}
            if listener_repair
            else {}
        ),
    }
    if image_update_target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET:
        result["unverifiedProviderIds"] = (
            ["microsoft_graph"] if account_center_graph_unissued else []
        )
        result["microsoftGraphStatus"] = graph_connection_status or "CONNECTED_VERIFIED"
    return result


def _read_live_exchange_readiness(
    *,
    plan: dict[str, Any],
    compose: dict[str, Any],
    container_id: str,
    image_id: str,
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Run the frozen read-only connector readiness CLI in the exact Exchange container."""

    exchange_plan = plan["candidateServices"].get("exchange")
    expected_image = (
        exchange_plan.get("candidate", {}).get("imageId")
        if isinstance(exchange_plan, dict)
        else plan["activeServices"].get("exchange", {}).get("imageId")
    )
    allowed_images = {expected_image}
    if plan.get("imageUpdateTarget") == EXCHANGE_IMAGE_UPDATE_TARGET:
        active_image = plan.get("activeServices", {}).get("exchange", {}).get("imageId")
        if isinstance(active_image, str):
            allowed_images.add(active_image)
    if image_id not in allowed_images or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError(
            "Exchange readiness target does not match its exact active/candidate image"
        )
    actor = preflight._connector_bootstrap_actor(compose)
    if any(not isinstance(value, str) or not value for value in actor.values()):
        raise StageTransitionError("candidate Exchange readiness actor is incomplete")
    argv = [
        *docker_prefix,
        "exec",
        container_id,
        EXCHANGE_SECRET_ENV_ENTRYPOINT,
        EXCHANGE_READINESS_EXECUTABLE,
        "integrations",
        "connector-readiness",
        "--integration-database",
        EXCHANGE_INTEGRATION_DATABASE,
        "--issuer",
        actor["issuer"],
        "--subject",
        actor["subject"],
        "--organization-id",
        actor["organizationId"],
        "--workspace-id",
        actor["workspaceId"],
    ]
    output = runtime_update._checked_stdout(
        argv,
        label="pinned Exchange connector readiness readback",
        timeout_seconds=120,
    )
    if len(output.encode("utf-8")) > 16 * 1024 * 1024:
        raise StageTransitionError("Exchange readiness output exceeds its bounded size")
    try:
        snapshot = json.loads(output, object_pairs_hook=preflight._unique_json_object)
    except (json.JSONDecodeError, ValueError) as error:
        raise StageTransitionError("pinned Exchange readiness output is invalid JSON") from error
    try:
        receipt = _verify_exchange_readiness_snapshot(
            snapshot=snapshot,
            compose=compose,
            phase=plan.get("phase", "issued-connector-activation"),
            image_update_target=plan.get("imageUpdateTarget"),
            listener_repair="listenerRepairState" in plan,
            include_live_catalog=True,
            account_center_database_state=(
                plan.get("accountCenterImageUpdateState", {}).get("database")
                if plan.get("imageUpdateTarget") == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
                and isinstance(plan.get("accountCenterImageUpdateState"), dict)
                else None
            ),
        )
    except StageTransitionError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise StageTransitionError(
            "pinned Exchange readiness projection is malformed or internally inconsistent"
        ) from error
    receipt["exchangeContainerId"] = container_id
    receipt["exchangeImageId"] = image_id
    receipt["snapshotSha256"] = _sha256_bytes(output.encode("utf-8"))
    return receipt


def _record_exchange_readiness(
    *,
    plan: dict[str, Any],
    compose: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> None:
    """Read readiness from the exact active or candidate Exchange selected by progress."""

    progress = journal["targetProgress"].get("exchange")
    uses_candidate = isinstance(progress, dict) and progress.get("candidateContainerId") is not None
    container_id = (
        progress.get("candidateContainerId")
        if uses_candidate
        else plan["activeServices"].get("exchange", {}).get("containerId")
    )
    image_id = (
        progress.get("candidateImageId")
        if uses_candidate
        else plan["activeServices"].get("exchange", {}).get("imageId")
    )
    receipt = _read_live_exchange_readiness(
        plan=plan,
        compose=compose,
        container_id=container_id,
        image_id=image_id,
        docker_prefix=docker_prefix,
    )
    if "listenerRepairState" in plan:
        expected_issued = plan["listenerRepairState"]["issuedState"]
        if (
            receipt.get("issuedStateSha256") != expected_issued["issuedStateSha256"]
            or receipt.get("status") != "LIVE_VERIFIED"
            or receipt.get("catalogStatus") != "LIVE_VERIFIED"
        ):
            raise StageTransitionError(
                "post-repair native Exchange readiness differs from the issued 14-connection baseline"
            )
    journal["exchangeReadiness"]["verified"] = True
    journal["exchangeReadiness"]["receipt"] = receipt
    _write_journal(journal_path, journal, "EXCHANGE_LIVE_READINESS_VERIFIED")


def _image_update_exchange_readiness_checkpoint(
    plan: dict[str, Any],
    journal: dict[str, Any],
    *,
    candidate_compose: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Require exact live Exchange bindings for image updates that depend on them."""

    target = plan.get("imageUpdateTarget")
    exchange = plan.get("activeServices", {}).get("exchange")
    receipt = journal.get("exchangeReadiness", {}).get("receipt")
    if not isinstance(receipt, dict):
        raise StageTransitionError("Exchange readiness receipt is missing")
    expected_scope_digests = plan.get("candidatePreflight", {}).get("expectedProviderScopeDigests")
    if not isinstance(expected_scope_digests, dict):
        raise StageTransitionError("candidate provider scope digests are malformed")
    expected_verified_provider_ids = sorted(expected_scope_digests)
    expected_binding_count = 14
    expected_profile_count: int | None = None
    expected_connection_count = 14
    if target == HUGGINGFACE_IMAGE_UPDATE_TARGET:
        connection_ref = (
            receipt.get("huggingfaceConnectionRef") if isinstance(receipt, dict) else None
        )
        connection_refs = {HUGGINGFACE_IMAGE_UPDATE_TARGET: connection_ref}
        expected_monitor_providers = [HUGGINGFACE_IMAGE_UPDATE_TARGET]
    elif (
        target == GITHUB_ISSUES_IMAGE_UPDATE_TARGET
        and plan.get("phase") == "issued-connector-activation"
    ):
        refs = receipt.get("githubConnectionRefs") if isinstance(receipt, dict) else None
        connection_refs = {"github": refs}
        expected_monitor_providers = ["github"]
    elif target == CLOUD_IMAGE_UPDATE_TARGET and plan.get("phase") == "issued-connector-activation":
        expected_monitor_providers = sorted(
            set(expected_scope_digests) & CLOUD_MONITOR_PROVIDER_IDS
        )
        if not {"azure", "gcp"} <= set(expected_monitor_providers):
            raise StageTransitionError(
                "Cloud image update readiness lacks Azure/GCP provider scopes"
            )
        refs = receipt.get("connectionRefByProvider") if isinstance(receipt, dict) else None
        connection_refs = refs if isinstance(refs, dict) else {}
    elif (
        target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
        and plan.get("phase") in ACCOUNT_CENTER_IMAGE_UPDATE_PHASES
    ):
        state = plan.get("accountCenterImageUpdateState")
        database_state = state.get("database") if isinstance(state, dict) else None
        _validate_account_center_database_state(database_state)
        if "microsoft_graph" in expected_scope_digests:
            raise StageTransitionError(
                "Account Center monitor scopes must exclude the unissued Microsoft Graph profile"
            )
        if not isinstance(candidate_compose, dict):
            raise StageTransitionError(
                "Account Center readiness checkpoint requires its frozen candidate Compose"
            )
        bindings_document = _candidate_config_json(candidate_compose, "connector-bindings")
        catalog_document = _candidate_config_json(candidate_compose, "connector-tool-catalog")
        graph_unissued = database_state["additionalMicrosoftProfileCount"] == 0
        inventory = _account_center_expected_exchange_inventory(
            compose=candidate_compose,
            phase=plan["phase"],
            graph_unissued=graph_unissued,
            bindings_document=bindings_document,
            catalog_document=catalog_document,
        )
        expected_monitor_providers = sorted(
            (set(expected_scope_digests) & {"huggingface", "azure", "gcp", "runpod"}) | {"github"}
        )
        if not {"github", "huggingface", "azure", "gcp"} <= set(expected_monitor_providers):
            raise StageTransitionError(
                "Account Center update requires the existing GitHub/HF/Azure/GCP connections"
            )
        refs = receipt.get("connectionRefByProvider") if isinstance(receipt, dict) else None
        if not isinstance(refs, dict) or set(refs) != set(expected_monitor_providers):
            raise StageTransitionError(
                "Account Center readiness lacks the exact existing non-Graph connection map"
            )
        connection_refs = dict(refs)
        expected_verified_provider_ids = sorted(inventory["activeProviderIds"])
        expected_binding_count = inventory["expectedBindingCount"]
        expected_profile_count = inventory["expectedProfileCount"]
        expected_connection_count = inventory["expectedConnectionCount"]
    elif (
        target == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET
        and plan.get("phase") in WORKSPACE_AUTHORITY_IMAGE_UPDATE_PHASES
    ) or (
        target == EXCHANGE_IMAGE_UPDATE_TARGET
        and plan.get("phase") == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
    ):
        if not isinstance(candidate_compose, dict):
            raise StageTransitionError(
                "Graph-bootstrap image readiness checkpoint requires its frozen candidate Compose"
            )
        expected_monitor_providers = sorted(set(expected_scope_digests) | {"microsoft_graph"})
        if not {"huggingface", "azure", "gcp", "microsoft_graph"} <= set(
            expected_monitor_providers
        ):
            raise StageTransitionError(
                "Graph-bootstrap image readiness requires the existing Graph/HF/Azure/GCP connections"
            )
        binding_document = _candidate_config_json(candidate_compose, "connector-bindings")
        binding_rows = _sorted_exchange_bindings(binding_document.get("bindings"))
        if plan["phase"] == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE:
            catalog_document = _candidate_config_json(candidate_compose, "connector-tool-catalog")
            if "microsoft_graph" in expected_scope_digests:
                raise StageTransitionError(
                    "Graph-bootstrap readiness must keep Graph outside the frozen scope inventory"
                )
            catalog_rows = _sorted_exchange_catalog(catalog_document)
            if any(row["providerId"] == "microsoft_graph" for row in binding_rows) or any(
                row["providerId"] == "microsoft_graph" for row in catalog_rows["items"]
            ):
                raise StageTransitionError(
                    "Graph-bootstrap image inputs must keep Graph bindings/catalog absent"
                )
            refs = receipt.get("connectionRefByProvider")
            profile_refs = receipt.get("profileRefByProvider")
            graph_connection_ref = refs.get("microsoft_graph") if isinstance(refs, dict) else None
            graph_profile_ref = (
                profile_refs.get("microsoft_graph") if isinstance(profile_refs, dict) else None
            )
            if (
                not isinstance(graph_connection_ref, str)
                or not isinstance(graph_profile_ref, str)
                or not graph_profile_ref
            ):
                raise StageTransitionError(
                    "Graph-bootstrap image readiness lacks the validated live Graph join"
                )
            connection_refs_from_bindings = [row["connectionRef"] for row in binding_rows]
            profile_refs_from_bindings = {row["profileRef"] for row in binding_rows}
            if (
                len(set(connection_refs_from_bindings)) != len(connection_refs_from_bindings)
                or graph_connection_ref in connection_refs_from_bindings
                or graph_profile_ref in profile_refs_from_bindings
            ):
                raise StageTransitionError(
                    "Graph-bootstrap image references must be unique from frozen bindings"
                )
            expected_binding_count = len(binding_rows) + 1
            expected_connection_count = len(
                set(connection_refs_from_bindings) | {graph_connection_ref}
            )
            expected_profile_count = len(
                {(row["providerId"], row["profileRef"]) for row in binding_rows}
                | {("microsoft_graph", graph_profile_ref)}
            )
        else:
            if sum(row["providerId"] == "microsoft_graph" for row in binding_rows) != 1:
                raise StageTransitionError(
                    "Workspace Authority readiness requires the one configured Microsoft Graph binding"
                )
            connection_refs_from_bindings = [row["connectionRef"] for row in binding_rows]
            if len(set(connection_refs_from_bindings)) != len(connection_refs_from_bindings):
                raise StageTransitionError(
                    "Workspace Authority candidate bindings must have unique connection references"
                )
            expected_binding_count = len(binding_rows)
            expected_connection_count = len(set(connection_refs_from_bindings))
            expected_profile_count = len(
                {(row["providerId"], row["profileRef"]) for row in binding_rows}
            )
        expected_verified_provider_ids = expected_monitor_providers
        refs = receipt.get("connectionRefByProvider")
        connection_refs = refs if isinstance(refs, dict) else {}
    else:
        raise StageTransitionError(
            "Exchange readiness checkpoint is outside its closed image-update route"
        )

    if set(connection_refs) != set(expected_monitor_providers):
        raise StageTransitionError(
            "Exchange readiness lacks the exact selected provider connections"
        )
    canonical_refs: dict[str, list[str]] = {}
    for provider, provider_refs in connection_refs.items():
        refs = provider_refs if isinstance(provider_refs, list) else [provider_refs]
        if (
            not refs
            or (provider == "github" and len(refs) != 10)
            or any(not isinstance(connection_ref, str) for connection_ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise StageTransitionError(
                f"live Exchange readiness lacks the exact selected {provider} connection refs"
            )
        canonical_provider_refs: list[str] = []
        for connection_ref in refs:
            try:
                canonical_connection_ref = str(uuid.UUID(connection_ref))
            except (ValueError, TypeError, AttributeError) as error:
                raise StageTransitionError(
                    f"live Exchange readiness lacks the exact selected {provider} connection"
                ) from error
            if canonical_connection_ref != connection_ref:
                raise StageTransitionError(f"live {provider} connection reference is not canonical")
            canonical_provider_refs.append(canonical_connection_ref)
        canonical_refs[provider] = sorted(canonical_provider_refs)
    if target == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET:
        state = plan.get("accountCenterImageUpdateState")
        database_state = state.get("database") if isinstance(state, dict) else None
        _validate_account_center_database_state(database_state)
        graph_unissued = database_state["additionalMicrosoftProfileCount"] == 0
        if receipt.get("unverifiedProviderIds") != (
            ["microsoft_graph"] if graph_unissued else []
        ) or receipt.get("microsoftGraphStatus") != (
            "NOT_CONNECTED_NOT_VERIFIED" if graph_unissued else "CONNECTED_VERIFIED"
        ):
            raise StageTransitionError(
                "Account Center Graph readiness does not match its sealed profile baseline"
            )
    expected_exchange = exchange
    if (
        target == EXCHANGE_IMAGE_UPDATE_TARGET
        and plan.get("phase") == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
    ):
        progress_records = journal.get("targetProgress")
        progress = (
            progress_records.get(EXCHANGE_IMAGE_UPDATE_TARGET)
            if isinstance(progress_records, dict)
            else None
        )
        candidate_exchange = plan.get("candidateServices", {}).get(EXCHANGE_IMAGE_UPDATE_TARGET)
        candidate_identity = (
            candidate_exchange.get("candidate") if isinstance(candidate_exchange, dict) else None
        )
        candidate_container_id = (
            progress.get("candidateContainerId") if isinstance(progress, dict) else None
        )
        candidate_image_id = (
            progress.get("candidateImageId") if isinstance(progress, dict) else None
        )
        if candidate_container_id is not None:
            if (
                not isinstance(candidate_container_id, str)
                or FULL_CONTAINER_ID_RE.fullmatch(candidate_container_id) is None
                or not isinstance(progress, dict)
                or progress.get("phase") != "APPLIED"
                or (
                    isinstance(exchange, dict)
                    and candidate_container_id == exchange.get("containerId")
                )
                or not isinstance(candidate_identity, dict)
                or candidate_image_id != candidate_identity.get("imageId")
            ):
                raise StageTransitionError(
                    "Exchange image update readiness lacks its exact applied candidate identity"
                )
            expected_exchange = {
                "containerId": candidate_container_id,
                "imageId": candidate_image_id,
            }
    if (
        not isinstance(expected_exchange, dict)
        or not isinstance(receipt, dict)
        or receipt.get("status") != "LIVE_VERIFIED"
        or receipt.get("catalogStatus") != "LIVE_VERIFIED"
        or receipt.get("exchangeContainerId") != expected_exchange.get("containerId")
        or receipt.get("exchangeImageId") != expected_exchange.get("imageId")
        or receipt.get("bindingCount") != expected_binding_count
        or receipt.get("connectionCount") != expected_connection_count
        or (
            expected_profile_count is not None
            and receipt.get("profileCount") != expected_profile_count
        )
        or receipt.get("verifiedProviderIds") != expected_verified_provider_ids
        or receipt.get("snapshotSha256") is None
        or SHA256_RE.fullmatch(str(receipt.get("snapshotSha256"))) is None
    ):
        raise StageTransitionError(
            "connector image update requires live readiness for the exact frozen Exchange inventory"
        )
    connection_ref_hashes = {
        provider: [_sha256_bytes(connection_ref.encode("utf-8")) for connection_ref in refs]
        if len(refs) > 1
        else _sha256_bytes(refs[0].encode("utf-8"))
        for provider, refs in sorted(canonical_refs.items())
    }
    return {
        "verified": True,
        "snapshotSha256": receipt["snapshotSha256"],
        "bindingCount": receipt["bindingCount"],
        **({"profileCount": receipt["profileCount"]} if expected_profile_count is not None else {}),
        "connectionCount": receipt["connectionCount"],
        "exchangeContainerId": receipt["exchangeContainerId"],
        "exchangeImageId": receipt["exchangeImageId"],
        "connectionRefsSha256": _sha256_bytes(_canonical_json(connection_ref_hashes)),
    }


def _make_journal(plan: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    """Create the initial secret-free durable transition journal."""

    return {
        "schemaVersion": JOURNAL_SCHEMA,
        "operationId": plan["operationId"],
        "planSha256": plan["planSha256"],
        "planPath": str(plan_path.resolve(strict=True)),
        "project": plan["project"],
        "phase": "PRECHECKED",
        "state": "RUNNING",
        "createdAtUtc": _utc_now(),
        "updatedAtUtc": _utc_now(),
        "targets": plan["targets"],
        "completedTargets": [],
        "edge": {
            "containerId": plan["edgeDelivery"]["containerId"],
            "initialRevision": plan["edgeDelivery"]["revision"],
            "initialDeadCount": plan["edgeDelivery"]["deadCount"],
            "latestDeadCount": plan["edgeDelivery"]["deadCount"],
            "pauseHolder": (
                None if plan.get("memoryUpdateTarget") is not None else plan["operationId"]
            ),
            "pauseIntent": False,
            "pauseRevision": None,
            "resumeRevision": None,
            "pauseHistory": [],
            "repauseExpectedRevision": None,
            "repauseHolder": None,
        },
        "navigator": {
            "initialContainerId": plan["navigatorDrain"]["containerId"],
            "initialInstanceId": plan["navigatorDrain"]["instanceId"],
            "initialGeneration": plan["navigatorDrain"]["generation"],
            "drainAttempt": None,
            "drainGeneration": None,
            "drained": False,
        },
        "database": {
            "required": plan.get("memoryUpdateTarget") is None,
            "backupPath": plan["database"]["backupPath"],
            "backupSha256": None,
            "snapshot": None,
            "preUpdateQuiescence": None,
            "postMigration": None,
            "restorePerformed": False,
        },
        "memoryUpdate": {
            "required": plan.get("memoryUpdateTarget") is not None,
            "target": plan.get("memoryUpdateTarget"),
            "plannedState": plan.get("memoryUpdateState"),
            "attemptIntent": False,
            "beforeReadback": None,
            "afterReadback": None,
            "beforeRuntimeLimits": None,
            "afterRuntimeLimits": None,
            "beforeExchangeReadiness": None,
            "afterExchangeReadiness": None,
            "outcome": "NOT_STARTED",
        },
        "exchangeReadiness": {
            "required": plan["exchangeReadiness"]["required"],
            "verified": False,
            "receipt": None,
        },
        "exchangeImageUpdate": {
            "required": plan.get("imageUpdateTarget") == EXCHANGE_IMAGE_UPDATE_TARGET
            and plan.get("phase") == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE,
            "beforeReadiness": None,
            "afterReadiness": None,
        },
        "huggingfaceImageUpdate": {
            "required": plan.get("imageUpdateTarget") == HUGGINGFACE_IMAGE_UPDATE_TARGET,
            "beforeReadiness": None,
            "monitorQuiescence": None,
            "afterReadiness": None,
        },
        "cloudImageUpdate": {
            "required": plan.get("imageUpdateTarget") == CLOUD_IMAGE_UPDATE_TARGET
            and plan.get("phase") == "issued-connector-activation",
            "beforeReadiness": None,
            "monitorQuiescence": None,
            "afterReadiness": None,
        },
        "githubIssuesImageUpdate": {
            "required": plan.get("imageUpdateTarget") == GITHUB_ISSUES_IMAGE_UPDATE_TARGET
            and plan.get("phase") == "issued-connector-activation",
            "beforeReadiness": None,
            "monitorQuiescence": None,
            "afterReadiness": None,
        },
        "workspaceAuthorityImageUpdate": {
            "required": plan.get("imageUpdateTarget") == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET,
            "beforeReadiness": None,
            "afterReadiness": None,
        },
        "accountCenterImageUpdate": {
            "required": plan.get("imageUpdateTarget") == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET,
            "databaseMutationInvoked": False,
            "beforeDatabaseIdentity": copy.deepcopy(
                plan.get("accountCenterImageUpdateState", {}).get("database")
            ),
            "afterDatabaseIdentity": None,
            "beforeRuntimeCapacity": copy.deepcopy(
                plan.get("accountCenterImageUpdateState", {}).get("runtimeCapacity")
            ),
            "afterRuntimeCapacity": None,
            "beforeReadiness": None,
            "monitorQuiescence": None,
            "afterMonitorQuiescence": None,
            "afterReadiness": None,
            "preservedUnknownPlan": copy.deepcopy(
                plan.get("accountCenterImageUpdateState", {}).get("preservedUnknown")
            ),
            "identityReadbackStatus": "PENDING",
        },
        "cloudRunnerIdle": {
            "required": "cloudRunnerState" in plan or "listenerRepairState" in plan,
            "planState": plan.get("cloudRunnerState"),
            "beforeTarget": None,
            "afterTarget": None,
            "postTargetReadbackFailed": False,
        },
        "listenerRepair": {
            "required": "listenerRepairState" in plan,
            "planState": plan.get("listenerRepairState"),
            "issuedStateBeforeTarget": None,
            "targetCommandsVerified": False,
        },
        "targetProgress": {
            service: {
                "phase": "PREPARED",
                "oldContainerId": plan["activeServices"].get(service, {}).get("containerId"),
                "oldImageId": plan["candidateServices"][service]["active"]["imageId"]
                if plan["candidateServices"][service]["active"] is not None
                else None,
                "candidateImageId": plan["candidateServices"][service]["candidate"]["imageId"],
                "added": plan["candidateServices"][service]["added"],
                "candidateContainerId": None,
                "applyIntent": False,
                "rollbackIntent": False,
                "rollbackContainerId": None,
            }
            for service in plan["targets"]
        },
        "selection": {
            "path": plan["candidateSelectionFile"],
            "backupPath": plan["selectionBackupPath"],
            "oldSha256": plan["activeSelection"]["sha256"],
            "newSha256": None,
            "promotionIntent": False,
            "promoted": False,
        },
        "resourceReadback": None,
        "lastErrorCode": None,
        "history": [{"phase": "PRECHECKED", "at": _utc_now()}],
    }


def _atomic_write_record(path: Path, document: dict[str, Any]) -> None:
    """Atomically replace an owner-only journal/selection without exposing partial JSON."""

    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.resolve(strict=True) != parent:
        raise StageTransitionError("transaction record parent directory is unsafe")
    parent_metadata = parent.stat()
    if parent_metadata.st_uid != os.geteuid() or stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        raise StageTransitionError(
            "transaction record parent must be owner-only and current-user-owned"
        )
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise StageTransitionError("transaction record path is unsafe")
    payload = _canonical_json(document) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise StageTransitionError(
            "transaction record could not be atomically persisted"
        ) from error


def _create_initial_journal(path: Path, document: dict[str, Any]) -> None:
    """Create one initial journal exclusively so a competing apply cannot replace it."""

    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.resolve(strict=True) != parent:
        raise StageTransitionError("transaction journal parent directory is unsafe")
    parent_metadata = parent.stat()
    if parent_metadata.st_uid != os.geteuid() or stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        raise StageTransitionError("transaction journal parent must be owner-only")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise StageTransitionError(
            "operation journal already exists; apply is never replayed"
        ) from error
    except OSError as error:
        raise StageTransitionError("initial transaction journal could not be created") from error
    payload = _canonical_json(document) + b"\n"
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StageTransitionError("initial transaction journal could not be persisted") from error


def _write_journal(
    path: Path, journal: dict[str, Any], phase: str, *, state: str | None = None
) -> None:
    """Persist phase intent before the next external side effect."""

    journal["phase"] = phase
    journal["updatedAtUtc"] = _utc_now()
    if state is not None:
        journal["state"] = state
    history = journal.get("history")
    if not isinstance(history, list):
        history = []
    history.append({"phase": phase, "at": journal["updatedAtUtc"]})
    journal["history"] = history[-200:]
    _atomic_write_record(path, journal)


def _read_journal(path: Path) -> dict[str, Any]:
    """Read a private stage transition journal and validate its identity fields."""

    try:
        metadata = path.lstat()
    except OSError as error:
        raise StageTransitionError("stage transition journal is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise StageTransitionError("stage transition journal must be an owner-only regular file")
    journal = _read_json(path, "stage transition journal")
    if (
        journal.get("schemaVersion") != JOURNAL_SCHEMA
        or not isinstance(journal.get("operationId"), str)
        or not isinstance(journal.get("planSha256"), str)
        or not SHA256_RE.fullmatch(journal["planSha256"])
        or not isinstance(journal.get("state"), str)
        or not isinstance(journal.get("phase"), str)
    ):
        raise StageTransitionError("stage transition journal is malformed")
    try:
        uuid.UUID(journal["operationId"])
    except ValueError as error:
        raise StageTransitionError("stage transition journal operation ID is invalid") from error
    return journal


def _journal_path(journal_dir: Path, operation_id: str) -> Path:
    """Return a UUID-constrained durable stage transition journal path."""

    try:
        canonical_id = str(uuid.UUID(operation_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise StageTransitionError("operation reference must be a UUID") from error
    return journal_dir / f"{canonical_id}.json"


def _journal_summary(journal: dict[str, Any]) -> dict[str, Any]:
    """Return safe transition status without paths, secrets, route values, or database names."""

    db = journal.get("database", {})
    database_snapshot = db.get("snapshot") if isinstance(db, dict) else None
    exchange_readiness = journal.get("exchangeReadiness", {})
    exchange_readiness_required = (
        exchange_readiness.get("required") is True
        if isinstance(exchange_readiness, dict)
        else False
    )
    exchange_readiness_verified = (
        exchange_readiness.get("verified") is True
        if isinstance(exchange_readiness, dict)
        else False
    )
    persistence_update_required = PERSISTENCE_IMAGE_UPDATE_TARGET in journal.get("targets", [])
    persistence_quiescence = db.get("preUpdateQuiescence") if isinstance(db, dict) else None
    persistence_post_update = db.get("postMigration") if isinstance(db, dict) else None
    persistence_compatibility = (
        persistence_post_update.get("migrationCompatibility")
        if isinstance(persistence_post_update, dict)
        else None
    )
    huggingface_update = journal.get("huggingfaceImageUpdate", {})
    huggingface_update_required = (
        huggingface_update.get("required") is True
        if isinstance(huggingface_update, dict)
        else False
    )
    huggingface_before = (
        huggingface_update.get("beforeReadiness") if isinstance(huggingface_update, dict) else None
    )
    huggingface_monitor = (
        huggingface_update.get("monitorQuiescence")
        if isinstance(huggingface_update, dict)
        else None
    )
    huggingface_after = (
        huggingface_update.get("afterReadiness") if isinstance(huggingface_update, dict) else None
    )
    huggingface_monitor_status = (
        "NOT_REQUIRED"
        if not huggingface_update_required
        else "VERIFIED"
        if isinstance(huggingface_monitor, dict) and huggingface_monitor.get("verified") is True
        else "BLOCKED"
        if isinstance(huggingface_monitor, dict) and huggingface_monitor.get("verified") is False
        else "PENDING"
    )
    huggingface_update_status = (
        "NOT_REQUIRED"
        if not huggingface_update_required
        else "VERIFIED"
        if isinstance(huggingface_before, dict)
        and huggingface_before.get("verified") is True
        and huggingface_monitor_status == "VERIFIED"
        and isinstance(huggingface_after, dict)
        and huggingface_after.get("verified") is True
        else "BLOCKED"
        if huggingface_monitor_status == "BLOCKED"
        else "PENDING"
    )
    cloud_update = journal.get("cloudImageUpdate", {})
    cloud_update_required = (
        cloud_update.get("required") is True if isinstance(cloud_update, dict) else False
    )
    cloud_before = cloud_update.get("beforeReadiness") if isinstance(cloud_update, dict) else None
    cloud_monitor = (
        cloud_update.get("monitorQuiescence") if isinstance(cloud_update, dict) else None
    )
    cloud_after = cloud_update.get("afterReadiness") if isinstance(cloud_update, dict) else None
    cloud_monitor_status = (
        "NOT_REQUIRED"
        if not cloud_update_required
        else "VERIFIED"
        if isinstance(cloud_monitor, dict) and cloud_monitor.get("verified") is True
        else "BLOCKED"
        if isinstance(cloud_monitor, dict) and cloud_monitor.get("verified") is False
        else "PENDING"
    )
    cloud_image_update_status = (
        "NOT_REQUIRED"
        if not cloud_update_required
        else "VERIFIED"
        if isinstance(cloud_before, dict)
        and cloud_before.get("verified") is True
        and cloud_monitor_status == "VERIFIED"
        and isinstance(cloud_after, dict)
        and cloud_after.get("verified") is True
        else "BLOCKED"
        if cloud_monitor_status == "BLOCKED"
        else "PENDING"
    )
    github_issues_update = journal.get("githubIssuesImageUpdate", {})
    github_issues_update_required = (
        github_issues_update.get("required") is True
        if isinstance(github_issues_update, dict)
        else False
    )
    github_issues_before = (
        github_issues_update.get("beforeReadiness")
        if isinstance(github_issues_update, dict)
        else None
    )
    github_issues_monitor = (
        github_issues_update.get("monitorQuiescence")
        if isinstance(github_issues_update, dict)
        else None
    )
    github_issues_after = (
        github_issues_update.get("afterReadiness")
        if isinstance(github_issues_update, dict)
        else None
    )
    github_issues_monitor_status = (
        "NOT_REQUIRED"
        if not github_issues_update_required
        else "VERIFIED"
        if isinstance(github_issues_monitor, dict) and github_issues_monitor.get("verified") is True
        else "BLOCKED"
        if isinstance(github_issues_monitor, dict)
        and github_issues_monitor.get("verified") is False
        else "PENDING"
    )
    github_issues_update_status = (
        "NOT_REQUIRED"
        if not github_issues_update_required
        else "VERIFIED"
        if isinstance(github_issues_before, dict)
        and github_issues_before.get("verified") is True
        and github_issues_monitor_status == "VERIFIED"
        and isinstance(github_issues_after, dict)
        and github_issues_after.get("verified") is True
        else "BLOCKED"
        if github_issues_monitor_status == "BLOCKED"
        else "PENDING"
    )
    authority_update = journal.get("workspaceAuthorityImageUpdate", {})
    authority_update_required = (
        authority_update.get("required") is True if isinstance(authority_update, dict) else False
    )
    authority_before = (
        authority_update.get("beforeReadiness") if isinstance(authority_update, dict) else None
    )
    authority_after = (
        authority_update.get("afterReadiness") if isinstance(authority_update, dict) else None
    )
    authority_update_status = (
        "NOT_REQUIRED"
        if not authority_update_required
        else "RECOVERY_REQUIRED"
        if journal.get("state") == "RECOVERY_REQUIRED"
        else "VERIFIED"
        if journal.get("state") == "APPLIED"
        and isinstance(authority_before, dict)
        and authority_before.get("verified") is True
        and isinstance(authority_after, dict)
        and authority_after.get("verified") is True
        else "BLOCKED"
        if (isinstance(authority_before, dict) and authority_before.get("verified") is False)
        or (isinstance(authority_after, dict) and authority_after.get("verified") is False)
        else "PENDING"
    )
    exchange_image_update = journal.get("exchangeImageUpdate", {})
    exchange_image_update_required = (
        exchange_image_update.get("required") is True
        if isinstance(exchange_image_update, dict)
        else False
    )
    exchange_image_before = (
        exchange_image_update.get("beforeReadiness")
        if isinstance(exchange_image_update, dict)
        else None
    )
    exchange_image_after = (
        exchange_image_update.get("afterReadiness")
        if isinstance(exchange_image_update, dict)
        else None
    )
    exchange_image_update_status = (
        "NOT_REQUIRED"
        if not exchange_image_update_required
        else "RECOVERY_REQUIRED"
        if journal.get("state") == "RECOVERY_REQUIRED"
        else "VERIFIED"
        if journal.get("state") == "APPLIED"
        and isinstance(exchange_image_before, dict)
        and exchange_image_before.get("verified") is True
        and isinstance(exchange_image_after, dict)
        and exchange_image_after.get("verified") is True
        else "BLOCKED"
        if (
            isinstance(exchange_image_before, dict)
            and exchange_image_before.get("verified") is False
        )
        or (
            isinstance(exchange_image_after, dict) and exchange_image_after.get("verified") is False
        )
        else "PENDING"
    )
    cloud_idle = journal.get("cloudRunnerIdle", {})
    cloud_idle_required = (
        cloud_idle.get("required") is True if isinstance(cloud_idle, dict) else False
    )
    cloud_idle_status = (
        "NOT_REQUIRED"
        if not cloud_idle_required
        else "RECOVERY_REQUIRED"
        if cloud_idle.get("postTargetReadbackFailed") is True
        else "IDLE_VERIFIED"
        if isinstance(cloud_idle.get("beforeTarget"), dict)
        and isinstance(cloud_idle.get("afterTarget"), dict)
        else "PRE_TARGET_VERIFIED"
        if isinstance(cloud_idle.get("beforeTarget"), dict)
        else "PENDING"
    )
    listener_repair = journal.get("listenerRepair", {})
    listener_repair_required = (
        listener_repair.get("required") is True if isinstance(listener_repair, dict) else False
    )
    listener_repair_status = (
        "NOT_REQUIRED"
        if not listener_repair_required
        else "LIVE_VERIFIED"
        if listener_repair.get("targetCommandsVerified") is True and exchange_readiness_verified
        else "ISSUED_STATE_VERIFIED"
        if isinstance(listener_repair.get("issuedStateBeforeTarget"), dict)
        else "PENDING"
    )
    memory_update = journal.get("memoryUpdate", {})
    memory_update_required = (
        memory_update.get("required") is True if isinstance(memory_update, dict) else False
    )
    memory_planned_state = (
        memory_update.get("plannedState") if isinstance(memory_update, dict) else None
    )
    memory_after = memory_update.get("afterReadback") if isinstance(memory_update, dict) else None
    memory_update_status = (
        "NOT_REQUIRED"
        if not memory_update_required
        else "VERIFIED"
        if journal.get("state") == "APPLIED" and isinstance(memory_after, dict)
        else "NO_MUTATION"
        if journal.get("state") == "FAILED_NO_MUTATION"
        else "RECOVERY_REQUIRED"
        if journal.get("state") == "RECOVERY_REQUIRED"
        else "PENDING"
    )
    account_center_update_record = journal.get("accountCenterImageUpdate", {})
    account_center_update = (
        account_center_update_record if isinstance(account_center_update_record, dict) else {}
    )
    account_center_update_required = account_center_update.get("required") is True
    account_center_monitor = (
        account_center_update.get("monitorQuiescence")
        if isinstance(account_center_update, dict)
        else None
    )
    account_center_after_monitor = (
        account_center_update.get("afterMonitorQuiescence")
        if isinstance(account_center_update, dict)
        else None
    )
    account_center_before = (
        account_center_update.get("beforeReadiness")
        if isinstance(account_center_update, dict)
        else None
    )
    account_center_after = (
        account_center_update.get("afterReadiness")
        if isinstance(account_center_update, dict)
        else None
    )
    account_center_unknown_expected = isinstance(
        account_center_update.get("preservedUnknownPlan"), dict
    )
    account_center_before_monitor_blocked = (
        isinstance(account_center_monitor, dict) and account_center_monitor.get("verified") is False
    )
    account_center_after_monitor_blocked = (
        isinstance(account_center_after_monitor, dict)
        and account_center_after_monitor.get("verified") is False
    )
    account_center_monitor_status = (
        "NOT_REQUIRED"
        if not account_center_update_required
        else "VERIFIED"
        if isinstance(account_center_monitor, dict)
        and account_center_monitor.get("verified") is True
        and isinstance(account_center_after_monitor, dict)
        and account_center_after_monitor.get("verified") is True
        else "BLOCKED"
        if account_center_before_monitor_blocked or account_center_after_monitor_blocked
        else "PENDING"
    )
    account_center_unknown_status = (
        "NOT_REQUIRED"
        if not account_center_unknown_expected
        else "PRESERVED_UNKNOWN_NOT_RESOLVED"
        if isinstance(account_center_monitor, dict)
        and account_center_monitor.get("preservedUnknownStatus") == "PRESERVED_UNKNOWN_NOT_RESOLVED"
        and isinstance(account_center_after_monitor, dict)
        and account_center_after_monitor.get("preservedUnknownStatus")
        == "PRESERVED_UNKNOWN_NOT_RESOLVED"
        and account_center_monitor.get("preservedUnknownRowSha256")
        == account_center_after_monitor.get("preservedUnknownRowSha256")
        else "BLOCKED"
        if account_center_monitor_status == "BLOCKED"
        else "PENDING"
    )
    account_center_update_status = (
        "NOT_REQUIRED"
        if not account_center_update_required
        else "RECOVERY_REQUIRED"
        if journal.get("state") == "RECOVERY_REQUIRED"
        else "VERIFIED"
        if journal.get("state") == "APPLIED"
        and account_center_update.get("identityReadbackStatus") == "VERIFIED"
        and account_center_monitor_status == "VERIFIED"
        and isinstance(account_center_before, dict)
        and account_center_before.get("verified") is True
        and isinstance(account_center_after, dict)
        and account_center_after.get("verified") is True
        else "BLOCKED"
        if account_center_monitor_status == "BLOCKED"
        else "PENDING"
    )
    account_center_before_database = (
        account_center_update.get("beforeDatabaseIdentity")
        if isinstance(account_center_update, dict)
        else None
    )
    return {
        "schemaVersion": journal.get("schemaVersion"),
        "operationId": journal.get("operationId"),
        "planSha256": journal.get("planSha256"),
        "project": journal.get("project"),
        "state": journal.get("state"),
        "phase": journal.get("phase"),
        "targets": journal.get("targets"),
        "completedTargets": journal.get("completedTargets"),
        "databaseOperation": (
            "NOT_REQUIRED_MEMORY_UPDATE"
            if memory_update_required
            else "READ_ONLY_BACKUP_AND_MIGRATION_GUARDS"
        ),
        "databaseBackupSha256": db.get("backupSha256") if isinstance(db, dict) else None,
        "databaseUserVersion": database_snapshot.get("userVersion")
        if isinstance(database_snapshot, dict)
        else None,
        "persistenceQuiescenceStatus": (
            "NOT_REQUIRED"
            if not persistence_update_required
            else "VERIFIED"
            if isinstance(persistence_quiescence, dict)
            and persistence_quiescence.get("verified") is True
            and persistence_quiescence.get("activeWriterLeaseCount") == 0
            else "PENDING"
        ),
        "persistenceSameSchemaStatus": (
            "NOT_REQUIRED"
            if not persistence_update_required
            else "VERIFIED"
            if isinstance(persistence_compatibility, dict)
            and persistence_compatibility.get("class") == "SAME_SCHEMA_AND_ALL_ROWS_PRESERVED"
            else "PENDING"
        ),
        "huggingfaceImageUpdateRequired": huggingface_update_required,
        "huggingfaceImageUpdateStatus": huggingface_update_status,
        "huggingfaceMonitorQuiescenceStatus": huggingface_monitor_status,
        "huggingfaceBeforeReadinessStatus": (
            "NOT_REQUIRED"
            if not huggingface_update_required
            else "LIVE_VERIFIED"
            if isinstance(huggingface_before, dict) and huggingface_before.get("verified") is True
            else "PENDING"
        ),
        "huggingfaceAfterReadinessStatus": (
            "NOT_REQUIRED"
            if not huggingface_update_required
            else "LIVE_VERIFIED"
            if isinstance(huggingface_after, dict) and huggingface_after.get("verified") is True
            else "PENDING"
        ),
        "cloudImageUpdateRequired": cloud_update_required,
        "cloudImageUpdateStatus": cloud_image_update_status,
        "cloudMonitorQuiescenceStatus": cloud_monitor_status,
        "cloudBeforeReadinessStatus": (
            "NOT_REQUIRED"
            if not cloud_update_required
            else "LIVE_VERIFIED"
            if isinstance(cloud_before, dict) and cloud_before.get("verified") is True
            else "PENDING"
        ),
        "cloudAfterReadinessStatus": (
            "NOT_REQUIRED"
            if not cloud_update_required
            else "LIVE_VERIFIED"
            if isinstance(cloud_after, dict) and cloud_after.get("verified") is True
            else "PENDING"
        ),
        "githubIssuesImageUpdateRequired": github_issues_update_required,
        "githubIssuesImageUpdateStatus": github_issues_update_status,
        "githubIssuesMonitorQuiescenceStatus": github_issues_monitor_status,
        "githubIssuesBeforeReadinessStatus": (
            "NOT_REQUIRED"
            if not github_issues_update_required
            else "LIVE_VERIFIED"
            if isinstance(github_issues_before, dict)
            and github_issues_before.get("verified") is True
            else "PENDING"
        ),
        "githubIssuesAfterReadinessStatus": (
            "NOT_REQUIRED"
            if not github_issues_update_required
            else "LIVE_VERIFIED"
            if isinstance(github_issues_after, dict) and github_issues_after.get("verified") is True
            else "PENDING"
        ),
        "workspaceAuthorityImageUpdateRequired": authority_update_required,
        "workspaceAuthorityImageUpdateStatus": authority_update_status,
        "workspaceAuthorityBeforeReadinessStatus": (
            "NOT_REQUIRED"
            if not authority_update_required
            else "LIVE_VERIFIED"
            if isinstance(authority_before, dict) and authority_before.get("verified") is True
            else "PENDING"
        ),
        "workspaceAuthorityAfterReadinessStatus": (
            "NOT_REQUIRED"
            if not authority_update_required
            else "LIVE_VERIFIED"
            if isinstance(authority_after, dict) and authority_after.get("verified") is True
            else "PENDING"
        ),
        "exchangeImageUpdateRequired": exchange_image_update_required,
        "exchangeImageUpdateStatus": exchange_image_update_status,
        "exchangeBeforeReadinessStatus": (
            "NOT_REQUIRED"
            if not exchange_image_update_required
            else "LIVE_VERIFIED"
            if isinstance(exchange_image_before, dict)
            and exchange_image_before.get("verified") is True
            else "PENDING"
        ),
        "exchangeAfterReadinessStatus": (
            "NOT_REQUIRED"
            if not exchange_image_update_required
            else "LIVE_VERIFIED"
            if isinstance(exchange_image_after, dict)
            and exchange_image_after.get("verified") is True
            else "PENDING"
        ),
        "accountCenterImageUpdateRequired": account_center_update_required,
        "accountCenterImageUpdateStatus": account_center_update_status,
        "accountCenterMonitorQuiescenceStatus": account_center_monitor_status,
        "accountCenterUnknownPreservationStatus": account_center_unknown_status,
        "accountCenterRawNonterminalConnectorInvocationCount": (
            account_center_after_monitor.get("rawNonterminalConnectorInvocationCount")
            if isinstance(account_center_after_monitor, dict)
            else None
        ),
        "accountCenterPreservedUnknownResultCount": (
            account_center_after_monitor.get("preservedUnknownResultCount")
            if isinstance(account_center_after_monitor, dict)
            else None
        ),
        "accountCenterActiveInFlightConnectorCallCount": (
            account_center_after_monitor.get("activeInFlightConnectorCallCount")
            if isinstance(account_center_after_monitor, dict)
            else None
        ),
        "accountCenterActiveOutboxJobCount": (
            account_center_after_monitor.get("activeOutboxJobCount")
            if isinstance(account_center_after_monitor, dict)
            else None
        ),
        "accountCenterActiveOutboxJobLeaseCount": (
            account_center_after_monitor.get("activeOutboxJobLeaseCount")
            if isinstance(account_center_after_monitor, dict)
            else None
        ),
        "accountCenterBeforeReadinessStatus": (
            "NOT_REQUIRED"
            if not account_center_update_required
            else "LIVE_VERIFIED"
            if isinstance(account_center_before, dict)
            and account_center_before.get("verified") is True
            else "PENDING"
        ),
        "accountCenterAfterReadinessStatus": (
            "NOT_REQUIRED"
            if not account_center_update_required
            else "LIVE_VERIFIED"
            if isinstance(account_center_after, dict)
            and account_center_after.get("verified") is True
            else "PENDING"
        ),
        "accountCenterMicrosoftGraphStatus": (
            account_center_after.get("microsoftGraphStatus")
            if isinstance(account_center_after, dict)
            else account_center_before.get("microsoftGraphStatus")
            if isinstance(account_center_before, dict)
            else None
        ),
        "accountCenterSchemaVersion": (
            account_center_before_database.get("schemaVersion")
            if isinstance(account_center_before_database, dict)
            else None
        ),
        "accountCenterProtectedProviderCount": (
            account_center_before_database.get("protectedProviderCount")
            if isinstance(account_center_before_database, dict)
            else None
        ),
        "accountCenterDatabaseMutationInvoked": (
            account_center_update.get("databaseMutationInvoked") is True
            if isinstance(account_center_update, dict)
            else False
        ),
        "exchangeReadinessRequired": exchange_readiness_required,
        "exchangeReadinessStatus": (
            "LIVE_VERIFIED"
            if exchange_readiness_verified
            else "PENDING"
            if exchange_readiness_required
            else "NOT_REQUIRED"
        ),
        "cloudRunnerIdleRequired": cloud_idle_required,
        "cloudRunnerIdleStatus": cloud_idle_status,
        "listenerRepairRequired": listener_repair_required,
        "listenerRepairStatus": listener_repair_status,
        "memoryUpdateRequired": memory_update_required,
        "memoryUpdateTarget": memory_update.get("target")
        if isinstance(memory_update, dict)
        else None,
        "memoryUpdateStatus": memory_update_status,
        "memoryUpdateDesiredBytes": (
            memory_planned_state.get("candidateMemoryBytes")
            if isinstance(memory_planned_state, dict)
            else None
        ),
        "memoryUpdateContainerPreserved": (
            memory_after.get("containerId") == memory_planned_state.get("containerId")
            if isinstance(memory_after, dict) and isinstance(memory_planned_state, dict)
            else None
        ),
        "composeConfigHashPreserved": (
            memory_after.get("composeConfigHash") == memory_planned_state.get("composeConfigHash")
            if isinstance(memory_after, dict) and isinstance(memory_planned_state, dict)
            else None
        ),
        "selectionPromoted": journal.get("selection", {}).get("promoted"),
        "updatedAtUtc": journal.get("updatedAtUtc"),
        "lastErrorCode": journal.get("lastErrorCode"),
    }


def _selection_document_for_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Build the exact stable runtime selector that survives container/host restarts."""

    return {
        "schemaVersion": SELECTION_SCHEMA,
        "project": plan["project"],
        "operationId": plan["operationId"],
        "planSha256": plan["planSha256"],
        "phase": plan["phase"],
        "composeFiles": plan["candidateInputs"]["composeFiles"],
        "envFiles": plan["candidateInputs"]["envFiles"],
        "imageLock": plan["candidateInputs"]["imageLock"],
        "secretsDir": plan["activeSecretsDir"],
        "configDir": plan["candidateConfigDir"],
        "huggingfaceSeed": plan["candidateInputs"]["huggingfaceSeed"],
        "runpodKey": plan["candidateInputs"]["runpodKey"],
        "runtimeConfigIdentity": plan["runtimeConfigIdentity"]["candidate"],
        "updatedAtUtc": _utc_now(),
    }


def _atomic_sqlite_snapshot(
    *, plan: dict[str, Any], docker_prefix: list[str], journal: dict[str, Any], journal_path: Path
) -> dict[str, Any]:
    """Create a consistent SQLite online backup through Persistence's Python sqlite3 API."""

    service = journal["database"]
    container_id = plan["activeServices"]["navigator-persistence"]["containerId"]
    backup_path = Path(plan["database"]["backupPath"])
    if backup_path.exists() or backup_path.is_symlink():
        raise StageTransitionError("SQLite backup destination already exists")
    backup_temp = f"/tmp/cyrene-stage-transition-{plan['operationId']}.sqlite3"
    source_path = plan["database"]["containerPath"]
    script = (
        _SQLITE_REPORT_PYTHON
        + "\nimport os\n"
        + "source_path,backup_path=sys.argv[1:3]\n"
        + "source=sqlite3.connect('file:'+source_path+'?mode=ro',uri=True)\n"
        + "target=sqlite3.connect(backup_path)\n"
        + "source.backup(target)\n"
        + "target.close();source.close();os.chmod(backup_path,0o600)\n"
        + "report=report_database(backup_path)\n"
        + "if report['integrity']!='ok': raise RuntimeError('SQLite backup integrity check failed')\n"
        + "report['backupFileSha256']=file_sha256(backup_path)\n"
        + "print(json.dumps(report,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False))\n"
    )
    service["backupIntent"] = True
    _write_journal(journal_path, journal, "DATABASE_BACKUP_REQUESTED")
    report_text = runtime_update._checked_stdout(
        [
            *docker_prefix,
            "exec",
            container_id,
            "/opt/venv/bin/python",
            "-c",
            script,
            source_path,
            backup_temp,
        ],
        label="Navigator Persistence SQLite online backup",
        timeout_seconds=120,
    )
    report = runtime_update._decode_json_output(report_text, "Navigator Persistence backup report")
    _validate_sqlite_report_shape(report, "Persistence online backup report")
    if (
        not isinstance(report.get("backupFileSha256"), str)
        or SHA256_RE.fullmatch(report["backupFileSha256"]) is None
    ):
        raise StageTransitionError("Persistence online backup file digest is malformed")
    copy_result = runtime_update._run_command(
        [*docker_prefix, "cp", f"{container_id}:{backup_temp}", str(backup_path)],
        timeout_seconds=120,
        external_mutation=True,
    )
    if copy_result.returncode != 0:
        raise StageTransitionError(
            "SQLite online backup could not be copied to its private host artifact"
        )
    os.chmod(backup_path, 0o600)
    if os.geteuid() == 0:
        os.chown(backup_path, 0, 0)
    backup_sha256 = _sha256_file(backup_path)
    try:
        host_db = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
        integrity = host_db.execute("PRAGMA integrity_check").fetchone()[0]
        user_version = host_db.execute("PRAGMA user_version").fetchone()[0]
        schema_rows = host_db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        host_db.close()
    except sqlite3.Error as error:
        raise StageTransitionError(
            "host SQLite online backup failed integrity verification"
        ) from error
    host_schema_objects = [{"type": row[0], "name": row[1], "sql": row[2]} for row in schema_rows]
    host_schema_digest = _sha256_bytes(_canonical_json(host_schema_objects))
    if (
        integrity != "ok"
        or user_version != report["userVersion"]
        or host_schema_digest != report["schemaSha256"]
        or backup_sha256 != report["backupFileSha256"]
    ):
        raise StageTransitionError("host SQLite backup differs from the container online snapshot")
    report["objects"] = sorted(report["objects"])
    report["integrity"] = integrity
    report["backupSha256"] = backup_sha256
    service["backupIntent"] = False
    service["backupSha256"] = backup_sha256
    service["snapshot"] = report
    service["restorePerformed"] = False
    _write_journal(journal_path, journal, "DATABASE_BACKED_UP")
    # The temporary file is confined to the Persistence container's /tmp and is not source data.
    cleanup_script = (
        "from pathlib import Path;Path(__import__('sys').argv[1]).unlink(missing_ok=True)"
    )
    try:
        runtime_update._run_command(
            [
                *docker_prefix,
                "exec",
                container_id,
                "/opt/venv/bin/python",
                "-c",
                cleanup_script,
                backup_temp,
            ],
            timeout_seconds=15,
        )
    except runtime_update.RuntimeUpdateError:
        # The completed host snapshot is durable; failure to remove only this /tmp copy
        # does not change SQLite state and container recreation will discard it.
        pass
    return report


def _verify_active_baseline(
    plan: dict[str, Any], docker_prefix: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-render and re-inspect the active topology before pausing ingress."""

    inputs = plan["activeInputs"]
    compose_files = [Path(row["path"]) for row in inputs["composeFiles"]]
    env_files = [Path(row["path"]) for row in inputs["envFiles"]]
    lock_path = Path(inputs["imageLock"]["path"])
    active_lock = runtime_update._load_lock(lock_path)
    try:
        compose = preflight._compose_config(docker_prefix, compose_files, env_files)
    except (OSError, preflight.PreflightError, subprocess.SubprocessError) as error:
        raise StageTransitionError("active Compose could not be re-rendered") from error
    if compose.get("name") != plan["project"]:
        raise StageTransitionError("active Compose project changed after planning")
    services = _live_service_inventory(
        compose=compose,
        image_lock=active_lock,
        docker_prefix=docker_prefix,
        mount_identity_by_service=plan["runtimeConfigIdentity"]["active"],
    )
    if services != plan["activeServices"]:
        raise StageTransitionError(
            "active service IDs/images/network inventory changed after planning"
        )
    if "cloudRunnerState" in plan:
        state = _validate_cloud_runner_state_record(
            plan.get("cloudRunnerState"), plan["activeServices"]
        )
        exchange = services.get("exchange")
        if not isinstance(exchange, dict):
            raise StageTransitionError("active Exchange service is missing for Cloud image update")
        current_state = _cloud_runner_state_record(
            active_compose=compose,
            candidate_compose=compose,
            exchange_service=exchange,
            docker_prefix=docker_prefix,
        )
        if current_state != state:
            raise StageTransitionError(
                "Exchange runner or durable cloud/email work is no longer idle for Cloud image update"
            )
    if "listenerRepairState" in plan:
        _verify_listener_repair_active_state(
            plan=plan,
            active_compose=compose,
            docker_prefix=docker_prefix,
        )
    edge = runtime_update._current_edge_status(
        docker_prefix, plan["edgeDelivery"]["containerId"], timeout_seconds=15
    )
    if (
        edge.get("paused") is not False
        or edge.get("revision") != plan["edgeDelivery"]["revision"]
        or edge.get("dead_count") != plan["edgeDelivery"]["deadCount"]
        or edge.get("dead_count") != 0
    ):
        raise StageTransitionError(
            "Teams ingress revision/dead-letter baseline changed after planning"
        )
    navigator = runtime_update._current_navigator_status(
        docker_prefix, plan["navigatorDrain"]["containerId"], timeout_seconds=15
    )
    if (
        navigator.get("mode") != "accepting"
        or navigator.get("instanceId") != plan["navigatorDrain"]["instanceId"]
        or navigator.get("generation") != plan["navigatorDrain"]["generation"]
    ):
        raise StageTransitionError("Navigator process/drain generation changed after planning")
    return compose, active_lock


def _candidate_configs(
    plan: dict[str, Any], docker_prefix: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-render the frozen candidate Compose and image lock for one apply."""

    inputs = plan["candidateInputs"]
    compose_files = [Path(row["path"]) for row in inputs["composeFiles"]]
    env_files = [Path(row["path"]) for row in inputs["envFiles"]]
    lock = runtime_update._load_lock(Path(inputs["imageLock"]["path"]))
    try:
        compose = preflight._compose_config(docker_prefix, compose_files, env_files)
    except (OSError, preflight.PreflightError, subprocess.SubprocessError) as error:
        raise StageTransitionError("candidate Compose could not be re-rendered") from error
    if compose.get("name") != plan["project"]:
        raise StageTransitionError("candidate Compose project changed after planning")
    if "cloudRunnerState" in plan:
        state = _validate_cloud_runner_state_record(
            plan.get("cloudRunnerState"), plan["activeServices"]
        )
        if _exchange_cloud_runner_compose_value(compose) != state["candidateComposeValue"]:
            raise StageTransitionError(
                "candidate Exchange cloud refresh runner setting changed after planning"
            )
    if "listenerRepairState" in plan:
        state = _validate_listener_repair_state_record(
            plan.get("listenerRepairState"), plan["activeServices"]
        )
        services = compose.get("services", {})
        for service_name, row in state["services"].items():
            service = services.get(service_name) if isinstance(services, dict) else None
            if not isinstance(service, dict) or service.get("command") != row["candidateCommand"]:
                raise StageTransitionError(
                    f"candidate {service_name} listener command changed after planning"
                )
    return compose, lock


def _compose_target_update(
    *,
    plan: dict[str, Any],
    service: str,
    use_candidate: bool,
    docker_prefix: list[str],
) -> subprocess.CompletedProcess[str]:
    """Run one ordered no-dependency, no-pull, no-build service replacement."""

    inputs = plan["candidateInputs"] if use_candidate else plan["activeInputs"]
    files = [Path(row["path"]) for row in inputs["composeFiles"]]
    env_files = [Path(row["path"]) for row in inputs["envFiles"]]
    if service == GITHUB_ISSUES_IMAGE_UPDATE_TARGET:
        # The V2 journal owns all gates; reuse the existing allowlisted GitHub Compose primitive.
        return runtime_update._compose_up(
            docker_prefix=docker_prefix,
            project=plan["project"],
            compose_files=files,
            env_files=env_files,
            service=service,
            timeout_seconds=plan["policy"]["healthTimeoutSeconds"],
        )
    return runtime_update._compose_up_stage_transition(
        docker_prefix=docker_prefix,
        project=plan["project"],
        compose_files=files,
        env_files=env_files,
        service=service,
        timeout_seconds=plan["policy"]["healthTimeoutSeconds"],
    )


def _update_target(
    *,
    plan: dict[str, Any],
    candidate_compose: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
    service_name: str,
) -> None:
    """Replace one service, persist its exact ID, then wait for its health read-back."""

    progress = journal["targetProgress"][service_name]
    if progress.get("phase") == "APPLIED":
        return
    candidate_service = _runtime_service_inventory(candidate_compose)[service_name]
    expected_image = plan["candidateServices"][service_name]["candidate"]
    progress["applyIntent"] = True
    progress["phase"] = "APPLY_REQUESTED"
    _write_journal(
        journal_path, journal, f"{service_name.upper().replace('-', '_')}_APPLY_REQUESTED"
    )
    try:
        result = _compose_target_update(
            plan=plan,
            service=service_name,
            use_candidate=True,
            docker_prefix=docker_prefix,
        )
    except runtime_update.RuntimeUpdateOutcomeUnknown as error:
        progress["phase"] = "APPLY_OUTCOME_UNKNOWN"
        journal["lastErrorCode"] = "docker_mutation_outcome_unknown"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        raise StageTransitionError(
            "Docker apply outcome is unknown; no service operation was replayed"
        ) from error
    except (OSError, runtime_update.RuntimeUpdateError, subprocess.SubprocessError) as error:
        progress["phase"] = "APPLY_OUTCOME_UNKNOWN"
        journal["lastErrorCode"] = "docker_mutation_outcome_unknown"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        raise StageTransitionError(
            "Docker apply did not return a safely reconcilable outcome"
        ) from error
    if result.returncode != 0:
        progress["phase"] = "APPLY_OUTCOME_UNKNOWN"
        journal["lastErrorCode"] = "docker_mutation_nonzero_exit"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        raise StageTransitionError(
            "Docker apply returned nonzero; no retry or automatic rollback was attempted"
        )
    try:
        current = _inspect_exact_service(
            project=plan["project"],
            service_name=service_name,
            expected_image=expected_image,
            has_healthcheck=bool(candidate_service.get("healthcheck")),
            docker_prefix=docker_prefix,
            allow_starting=True,
        )
    except StageTransitionError as error:
        progress["phase"] = "APPLY_READBACK_UNKNOWN"
        journal["lastErrorCode"] = "docker_mutation_readback_unknown"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        raise StageTransitionError(
            "Docker succeeded but exact service read-back is ambiguous"
        ) from error
    old_id = progress.get("oldContainerId")
    if isinstance(old_id, str) and current["containerId"] == old_id:
        progress["phase"] = "APPLY_READBACK_UNKNOWN"
        journal["lastErrorCode"] = "candidate_container_not_replaced"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        raise StageTransitionError("Compose did not replace the exact target container")
    progress["candidateContainerId"] = current["containerId"]
    progress["phase"] = "CONTAINER_READBACK"
    _write_journal(
        journal_path, journal, f"{service_name.upper().replace('-', '_')}_CONTAINER_READBACK"
    )
    current = _wait_for_target_service(
        project=plan["project"],
        service_name=service_name,
        expected_image=expected_image,
        has_healthcheck=bool(candidate_service.get("healthcheck")),
        timeout_seconds=plan["policy"]["healthTimeoutSeconds"],
        docker_prefix=docker_prefix,
    )
    progress["phase"] = "APPLIED"
    progress["applyIntent"] = False
    progress["healthyContainerId"] = current["containerId"]
    journal["completedTargets"].append(service_name)
    _write_journal(journal_path, journal, f"{service_name.upper().replace('-', '_')}_APPLIED")


def _apply_memory_update(
    *,
    plan: dict[str, Any],
    active_compose: dict[str, Any],
    candidate_compose: dict[str, Any],
    candidate_lock: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Increase one running container's memory without pausing service traffic.

    中文：只调用 Docker 的单容器 update；任何不确定结果都只读核对，不回降或重放。
    """

    target = plan["memoryUpdateTarget"]
    planned = plan["memoryUpdateState"]
    active_service = plan["activeServices"][target]
    progress = journal["targetProgress"][target]
    memory_update = journal["memoryUpdate"]

    def mark_recovery(error_code: str, phase: str) -> None:
        memory_update["outcome"] = "RECOVERY_REQUIRED"
        journal["lastErrorCode"] = error_code
        _write_journal(journal_path, journal, phase, state="RECOVERY_REQUIRED")

    def inspect_target() -> dict[str, Any]:
        return _memory_update_container_snapshot(
            container_id=active_service["containerId"],
            project=plan["project"],
            service_name=target,
            expected_image={
                "imageRef": active_service["imageRef"],
                "imageId": active_service["imageId"],
            },
            expected_mounts=plan["runtimeConfigIdentity"]["active"][target],
            expected_network_names=active_service["networkNames"],
            docker_prefix=docker_prefix,
        )

    def mark_no_mutation(error_code: str, phase: str) -> None:
        progress["phase"] = "FAILED_NO_MUTATION"
        progress["applyIntent"] = False
        memory_update["outcome"] = "FAILED_NO_MUTATION"
        journal["lastErrorCode"] = error_code
        _write_journal(journal_path, journal, phase, state="FAILED_NO_MUTATION")

    # Read-only provider and target checks finish before the one bounded Docker update.
    try:
        _record_exchange_readiness(
            plan=plan,
            compose=candidate_compose,
            journal=journal,
            journal_path=journal_path,
            docker_prefix=docker_prefix,
        )
        memory_update["beforeExchangeReadiness"] = copy.deepcopy(
            journal["exchangeReadiness"]["receipt"]
        )
        _write_journal(journal_path, journal, "MEMORY_UPDATE_EXCHANGE_READINESS_BEFORE_VERIFIED")
        before = inspect_target()
        _verify_memory_update_snapshot(
            before,
            planned,
            active_service,
            expected_memory_bytes=planned["activeMemoryBytes"],
        )
        _verify_memory_update_config_hash_relation(plan=plan, snapshot=before)
        active_limits = _memory_update_actual_capacity(
            compose=active_compose,
            services=plan["activeServices"],
            max_memory_mib=plan["candidatePreflight"]["maxMemoryMiB"],
            max_cpus=plan["candidatePreflight"]["maxCpus"],
            docker_prefix=docker_prefix,
        )
        if (
            active_limits["totalMemoryBytes"] != planned["activeRuntimeMemoryBytes"]
            or active_limits["totalNanoCpus"] != planned["activeRuntimeNanoCpus"]
        ):
            raise StageTransitionError("actual 14-service resource limits changed after planning")
        memory_update["beforeRuntimeLimits"] = active_limits
        edge_status = runtime_update._current_edge_status(
            docker_prefix, plan["edgeDelivery"]["containerId"], timeout_seconds=15
        )
        if (
            edge_status.get("paused") is not False
            or edge_status.get("revision") != plan["edgeDelivery"]["revision"]
            or edge_status.get("dead_count") != plan["edgeDelivery"]["deadCount"]
        ):
            raise StageTransitionError("Teams ingress delivery state changed before memory update")
        navigator_status = runtime_update._current_navigator_status(
            docker_prefix, plan["navigatorDrain"]["containerId"], timeout_seconds=15
        )
        if (
            navigator_status.get("mode") != "accepting"
            or navigator_status.get("instanceId") != plan["navigatorDrain"]["instanceId"]
            or navigator_status.get("generation") != plan["navigatorDrain"]["generation"]
        ):
            raise StageTransitionError(
                "Navigator is not accepting on its planned process generation"
            )
    except (
        OSError,
        StageTransitionError,
        runtime_update.RuntimeUpdateError,
        preflight.PreflightError,
        subprocess.SubprocessError,
    ) as error:
        mark_no_mutation(
            "memory_update_pre_mutation_gate_failed",
            "MEMORY_UPDATE_BLOCKED_BEFORE_MUTATION",
        )
        raise StageTransitionError(
            "memory update failed a read-only gate before mutation"
        ) from error

    memory_update["beforeReadback"] = before
    memory_update["attemptIntent"] = True
    memory_update["outcome"] = "UPDATE_INTENT_RECORDED"
    progress["applyIntent"] = True
    progress["phase"] = "APPLY_REQUESTED"
    _write_journal(journal_path, journal, "MEMORY_UPDATE_DOCKER_UPDATE_INTENT_RECORDED")

    # Docker Engine updates these cgroup limits in place; Compose up would recreate the service.
    command_error: BaseException | None = None
    command_outcome_unknown = False
    try:
        update_result = runtime_update._run_command(
            [
                *docker_prefix,
                "update",
                "--memory",
                str(planned["candidateMemoryBytes"]),
                "--memory-swap",
                str(planned["candidateMemoryBytes"]),
                active_service["containerId"],
            ],
            timeout_seconds=30,
            external_mutation=True,
        )
    except runtime_update.RuntimeUpdateOutcomeUnknown as error:
        command_error = error
        command_outcome_unknown = True
    except (OSError, runtime_update.RuntimeUpdateError, subprocess.SubprocessError) as error:
        command_error = error

    # Reconcile exactly once after the request; never retry or lower a possibly raised limit.
    try:
        observed = inspect_target()
    except (
        OSError,
        StageTransitionError,
        runtime_update.RuntimeUpdateError,
        subprocess.SubprocessError,
    ) as error:
        mark_recovery("memory_update_readback_unavailable", "MEMORY_UPDATE_OUTCOME_UNKNOWN")
        raise StageTransitionError(
            "memory update outcome is unknown; no retry or rollback was attempted"
        ) from error
    try:
        _verify_memory_update_snapshot(
            observed,
            planned,
            active_service,
            expected_memory_bytes=planned["candidateMemoryBytes"],
        )
    except StageTransitionError as desired_state_error:
        try:
            _verify_memory_update_snapshot(
                observed,
                planned,
                active_service,
                expected_memory_bytes=planned["activeMemoryBytes"],
            )
        except StageTransitionError as partial_state_error:
            memory_update["afterReadback"] = observed
            mark_recovery(
                "memory_update_partial_or_identity_mismatch",
                "MEMORY_UPDATE_PARTIAL_READBACK_RECOVERY_REQUIRED",
            )
            raise StageTransitionError(
                "memory update is partial or its identity changed; leave the limit untouched for recovery"
            ) from partial_state_error
        if command_outcome_unknown:
            memory_update["afterReadback"] = observed
            mark_recovery(
                "memory_update_timed_out_readback_not_final",
                "MEMORY_UPDATE_TIMEOUT_READBACK_RECOVERY_REQUIRED",
            )
            raise StageTransitionError(
                "Docker update timed out; the old limits were observed once but the daemon outcome may still complete"
            ) from command_error
        mark_no_mutation(
            "memory_update_command_failed_without_change",
            "MEMORY_UPDATE_FAILED_NO_MUTATION",
        )
        message = (
            "Docker memory update returned an error; exact old limits remain"
            if command_error is not None or update_result.returncode != 0
            else "Docker returned success but exact old limits remain"
        )
        raise StageTransitionError(message + "; no retry was attempted") from (
            command_error or desired_state_error
        )

    memory_update["outcome"] = (
        "READBACK_CONFIRMED_APPLIED"
        if command_error is not None or update_result.returncode != 0
        else "APPLY_AND_READBACK_VERIFIED"
    )
    memory_update["afterReadback"] = observed

    progress["candidateContainerId"] = active_service["containerId"]
    progress["healthyContainerId"] = active_service["containerId"]
    progress["phase"] = "TARGET_READBACK_VERIFIED"
    progress["applyIntent"] = False
    _write_journal(journal_path, journal, "MEMORY_UPDATE_TARGET_READBACK_VERIFIED")

    try:
        candidate_services = _live_service_inventory(
            compose=candidate_compose,
            image_lock=candidate_lock,
            docker_prefix=docker_prefix,
            mount_identity_by_service=plan["runtimeConfigIdentity"]["candidate"],
        )
        if candidate_services != plan["activeServices"]:
            raise StageTransitionError(
                "memory update changed a container ID, image, network, health, or mount identity"
            )
        runtime_update._verify_live_network_membership(
            candidate_compose, candidate_services, docker_prefix
        )
        candidate_limits = _memory_update_actual_capacity(
            compose=candidate_compose,
            services=candidate_services,
            max_memory_mib=plan["candidatePreflight"]["maxMemoryMiB"],
            max_cpus=plan["candidatePreflight"]["maxCpus"],
            docker_prefix=docker_prefix,
        )
        expected_total_bytes = planned["activeRuntimeMemoryBytes"] + (
            planned["candidateMemoryBytes"] - planned["activeMemoryBytes"]
        )
        if (
            candidate_limits["totalMemoryBytes"] != expected_total_bytes
            or candidate_limits["totalNanoCpus"] != planned["activeRuntimeNanoCpus"]
        ):
            raise StageTransitionError(
                "actual 14-service resource limits differ from the exact increase"
            )
        memory_update["afterRuntimeLimits"] = candidate_limits
        _record_exchange_readiness(
            plan=plan,
            compose=candidate_compose,
            journal=journal,
            journal_path=journal_path,
            docker_prefix=docker_prefix,
        )
        memory_update["afterExchangeReadiness"] = copy.deepcopy(
            journal["exchangeReadiness"]["receipt"]
        )
        _write_journal(journal_path, journal, "MEMORY_UPDATE_EXCHANGE_READINESS_AFTER_VERIFIED")
        journal["resourceReadback"] = _validate_post_update_capacity(
            compose=candidate_compose,
            services=candidate_services,
            docker_prefix=docker_prefix,
            minimum_available_mib=plan["candidatePreflight"]["minimumMemAvailableMiB"],
        )
        _write_journal(journal_path, journal, "MEMORY_UPDATE_HOST_CAPACITY_READBACK_VERIFIED")
        edge_status = runtime_update._current_edge_status(
            docker_prefix, plan["edgeDelivery"]["containerId"], timeout_seconds=15
        )
        runtime_update._verify_edge_dead_count(
            journal["edge"], edge_status.get("dead_count"), "memory update readback"
        )
        if (
            edge_status.get("paused") is not False
            or edge_status.get("revision") != plan["edgeDelivery"]["revision"]
        ):
            raise StageTransitionError("Teams ingress was paused or revised during memory update")
        navigator_status = runtime_update._current_navigator_status(
            docker_prefix, plan["navigatorDrain"]["containerId"], timeout_seconds=15
        )
        if (
            navigator_status.get("mode") != "accepting"
            or navigator_status.get("instanceId") != plan["navigatorDrain"]["instanceId"]
            or navigator_status.get("generation") != plan["navigatorDrain"]["generation"]
        ):
            raise StageTransitionError("Navigator process changed during memory update")
        selection = _promote_selection(
            plan=plan,
            journal=journal,
            journal_path=journal_path,
        )
    except (
        OSError,
        StageTransitionError,
        runtime_update.RuntimeUpdateError,
        preflight.PreflightError,
        subprocess.SubprocessError,
    ) as error:
        mark_recovery("memory_update_post_apply_readback_failed", "MEMORY_UPDATE_RECOVERY_REQUIRED")
        raise StageTransitionError(
            "memory was increased, but a later readiness or selector read-back failed; do not lower it automatically"
        ) from error

    progress["phase"] = "APPLIED"
    progress["applyIntent"] = False
    if target not in journal["completedTargets"]:
        journal["completedTargets"].append(target)
    memory_update["outcome"] = "APPLIED_SAME_CONTAINER"
    _write_journal(journal_path, journal, "APPLIED", state="APPLIED")
    return {
        **_journal_summary(journal),
        "activeRuntimeSelection": _selection_summary(selection),
    }


def _current_selection_digest(path: Path) -> str | None:
    """Read only the current stable selector digest for local promotion reconciliation."""

    if path.is_symlink():
        raise StageTransitionError("runtime selection path became a symlink")
    return _sha256_file(path) if path.exists() else None


def _container_exists(container_id: str, docker_prefix: list[str]) -> bool:
    """Read Docker's exact-ID inventory; fail closed on an unavailable daemon."""

    if FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError("container identity is malformed")
    output = runtime_update._checked_stdout(
        [
            *docker_prefix,
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"id={container_id}",
            "--format",
            "{{.ID}}",
        ],
        label="operation-owned container presence readback",
    )
    ids = [value.strip() for value in output.splitlines() if value.strip()]
    if any(FULL_CONTAINER_ID_RE.fullmatch(value) is None for value in ids):
        raise StageTransitionError("Docker returned a malformed exact-ID inventory")
    if not ids:
        return False
    if ids != [container_id]:
        raise StageTransitionError("Docker exact-ID inventory is ambiguous")
    return True


def _selection_promotion_uncertain(journal: dict[str, Any]) -> bool:
    """Detect a selector replacement that began but was not durably reconciled."""

    selection = journal.get("selection", {})
    if not isinstance(selection, dict):
        return True
    if selection.get("promoted") is True:
        return True
    if selection.get("promotionIntent") is not True:
        return False
    path_text = selection.get("path")
    if not isinstance(path_text, str):
        return True
    try:
        current = _current_selection_digest(Path(path_text))
    except (OSError, StageTransitionError):
        return True
    return current != selection.get("oldSha256")


def _promote_selection(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
) -> dict[str, Any]:
    """Archive the old stable selector and atomically promote candidate desired state."""

    selection = journal["selection"]
    selection_path = Path(selection["path"])
    before_digest = _current_selection_digest(selection_path)
    if before_digest != selection.get("oldSha256"):
        raise StageTransitionError("active runtime selector changed before candidate promotion")
    active_doc = plan["activeSelection"].get("document")
    backup_path = Path(selection["backupPath"])
    if active_doc is not None:
        if backup_path.exists() or backup_path.is_symlink():
            raise StageTransitionError("runtime selector backup already exists")
        try:
            runtime_update._write_private_json(backup_path, active_doc, require_artifact=True)
        except runtime_update.RuntimeUpdateError as error:
            raise StageTransitionError("prior runtime selection could not be archived") from error
    selection_doc = _selection_document_for_plan(plan)
    selection_bytes = _canonical_json(selection_doc) + b"\n"
    selection["newSha256"] = _sha256_bytes(selection_bytes)
    selection["promotionIntent"] = True
    _write_journal(journal_path, journal, "RUNTIME_SELECTION_PROMOTION_REQUESTED")
    _atomic_write_record(selection_path, selection_doc)
    if _current_selection_digest(selection_path) != selection["newSha256"]:
        journal["lastErrorCode"] = "runtime_selection_readback_mismatch"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        raise StageTransitionError("candidate runtime selector failed its atomic read-back")
    selection["promotionIntent"] = False
    selection["promoted"] = True
    _write_journal(journal_path, journal, "RUNTIME_SELECTION_PROMOTED")
    return selection_doc


def _resume_live_navigator(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    candidate_compose: dict[str, Any],
    docker_prefix: list[str],
) -> None:
    """Require the replacement executor process to be accepting before ingress is resumed."""

    current = _live_service_inventory(
        compose=candidate_compose,
        image_lock=runtime_update._load_lock(Path(plan["candidateInputs"]["imageLock"]["path"])),
        docker_prefix=docker_prefix,
        mount_identity_by_service=plan["runtimeConfigIdentity"]["candidate"],
    )
    nav = current.get("navigator")
    if not isinstance(nav, dict):
        raise StageTransitionError("candidate Navigator container is missing")
    status = runtime_update._current_navigator_status(
        docker_prefix, nav["containerId"], timeout_seconds=15
    )
    if (
        status.get("mode") != "accepting"
        or status.get("instanceId") == journal["navigator"]["initialInstanceId"]
    ):
        raise StageTransitionError(
            "replacement Navigator is not accepting in a fresh process instance"
        )


def _resume_old_executor_if_needed(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    docker_prefix: list[str],
    require_original_instance: bool = False,
) -> None:
    """Resume only the exact old drained process when no Navigator container was replaced."""

    navigator = journal.get("navigator")
    if not isinstance(navigator, dict):
        raise StageTransitionError("Navigator recovery identity is malformed")
    progress = journal.get("targetProgress", {}).get("navigator", {})
    if isinstance(progress, dict) and progress.get("phase") == "APPLIED":
        return
    initial_container_id = navigator.get("initialContainerId")
    initial_instance_id = navigator.get("initialInstanceId")
    initial_generation = navigator.get("initialGeneration")
    if (
        not isinstance(initial_container_id, str)
        or not isinstance(initial_instance_id, str)
        or isinstance(initial_generation, bool)
        or not isinstance(initial_generation, int)
        or initial_generation < 0
    ):
        raise StageTransitionError("Navigator recovery identity is malformed")
    drain_generation = initial_generation + 1
    expected_drain_attempt = {
        "containerId": initial_container_id,
        "instanceId": initial_instance_id,
        "initialGeneration": initial_generation,
        "generation": drain_generation,
    }
    drain_attempt = navigator.get("drainAttempt")
    journaled_drain_generation = navigator.get("drainGeneration")
    if drain_attempt is not None and drain_attempt != expected_drain_attempt:
        raise StageTransitionError("Navigator drain intent does not match the reviewed plan")
    if journaled_drain_generation not in (None, drain_generation):
        raise StageTransitionError("Navigator drain generation does not match the reviewed plan")
    containers = runtime_update._list_project_navigator_containers(
        project=plan["project"], docker_prefix=docker_prefix
    )
    running = [row for row in containers if row.get("running") is True]
    if len(running) != 1 or not isinstance(running[0].get("containerId"), str):
        raise StageTransitionError("Navigator recovery inventory is ambiguous")
    current_id = running[0]["containerId"]
    status = runtime_update._current_navigator_status(
        docker_prefix,
        current_id,
        timeout_seconds=15,
    )
    if require_original_instance:
        if current_id != initial_container_id:
            raise StageTransitionError(
                "Navigator recovery container differs from the original instance"
            )
        if status.get("instanceId") != initial_instance_id:
            raise StageTransitionError(
                "Navigator recovery process differs from the original instance"
            )
        if status.get("mode") == "draining":
            if (
                status.get("generation") != drain_generation
                or drain_attempt != expected_drain_attempt
            ):
                raise StageTransitionError(
                    "old Navigator drain generation cannot be safely resumed"
                )
            runtime_update._resume_navigator_instance(
                docker_prefix=docker_prefix,
                container_id=current_id,
                instance_id=initial_instance_id,
                generation=drain_generation,
                timeout_seconds=30,
            )
        elif status.get("mode") == "accepting":
            if (
                status.get("generation") == initial_generation
                and journaled_drain_generation is None
            ):
                return
            if (
                status.get("generation") == drain_generation
                and drain_attempt == expected_drain_attempt
            ):
                return
            raise StageTransitionError(
                "accepting Navigator is outside the known original generation"
            )
        else:
            raise StageTransitionError("Navigator did not return to an accepting generation")
        return

    if current_id != initial_container_id and not (
        isinstance(progress, dict)
        and progress.get("phase") == "ROLLED_BACK"
        and progress.get("rollbackContainerId") == current_id
    ):
        raise StageTransitionError("Navigator recovery container is not recorded by this operation")
    if current_id == initial_container_id and status.get("mode") == "draining":
        if (
            status.get("generation") != drain_generation
            or status.get("instanceId") != initial_instance_id
        ):
            raise StageTransitionError("old Navigator drain generation cannot be safely resumed")
        runtime_update._resume_navigator_instance(
            docker_prefix=docker_prefix,
            container_id=current_id,
            instance_id=initial_instance_id,
            generation=drain_generation,
            timeout_seconds=30,
        )
    elif status.get("mode") != "accepting":
        raise StageTransitionError("Navigator did not return to an accepting generation")


def _resume_executor_after_transition(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    candidate_compose: dict[str, Any],
    docker_prefix: list[str],
) -> None:
    """Keep the original Navigator generation for non-Navigator target updates.

    中文：Exchange、Cloud 或 listener repair 后恢复原 Navigator 容器与 generation。
    """

    if plan.get("imageUpdateTarget") in PRESERVE_NAVIGATOR_IMAGE_TARGETS or (
        "listenerRepairState" in plan
    ):
        _resume_old_executor_if_needed(
            plan=plan,
            journal=journal,
            docker_prefix=docker_prefix,
            require_original_instance=True,
        )
        return
    _resume_live_navigator(
        plan=plan,
        journal=journal,
        candidate_compose=candidate_compose,
        docker_prefix=docker_prefix,
    )


def _resume_ingress_after_safe_state(
    *, journal: dict[str, Any], journal_path: Path, docker_prefix: list[str]
) -> None:
    """Release only this operation's durable ingress pause after service state is safe."""

    if (
        journal.get("edge", {}).get("pauseIntent") is True
        and journal["edge"].get("pauseRevision") is None
    ):
        runtime_update._recover_edge_pause(
            journal=journal,
            journal_path=journal_path,
            docker_prefix=docker_prefix,
            timeout_seconds=30,
        )
    runtime_update._resume_edge(
        journal=journal,
        journal_path=journal_path,
        docker_prefix=docker_prefix,
    )


def _verify_final_delivery_readback(
    *, journal: dict[str, Any], docker_prefix: list[str]
) -> dict[str, Any]:
    """Require the final resumed revision and unchanged DLQ count before APPLIED."""

    edge = journal["edge"]
    status = runtime_update._current_edge_status(docker_prefix, edge["containerId"])
    runtime_update._verify_edge_dead_count(edge, status["dead_count"], "final APPLIED readback")
    if (
        status["paused"] is not False
        or status["revision"] != edge.get("resumeRevision")
        or edge.get("resumeRevision") is None
    ):
        raise StageTransitionError("Teams ingress final resume revision changed before APPLIED")
    return status


def _account_center_image_downgrade_may_be_unsafe(
    plan: dict[str, Any], progress: dict[str, Any]
) -> bool:
    """Detect a started Account Center candidate whose old image cannot read migrated state."""

    migration_may_have_started = (
        progress.get("applyIntent") is True
        or progress.get("rollbackIntent") is True
        or isinstance(progress.get("candidateContainerId"), str)
    )
    if not migration_may_have_started:
        return False
    target = plan.get("candidateServices", {}).get("account-center", {})
    active_image = target.get("active") if isinstance(target, dict) else None
    candidate_image = target.get("candidate") if isinstance(target, dict) else None
    if active_image is None:
        return True
    if (
        not isinstance(active_image, dict)
        or not isinstance(candidate_image, dict)
        or not isinstance(active_image.get("imageId"), str)
        or not isinstance(candidate_image.get("imageId"), str)
    ):
        return True
    return active_image["imageId"] != candidate_image["imageId"]


def _persistence_image_update_may_have_started(
    plan: dict[str, Any], journal: dict[str, Any]
) -> bool:
    """Detect any Persistence replacement intent that must never be auto-rolled back."""

    if plan.get("imageUpdateTarget") != PERSISTENCE_IMAGE_UPDATE_TARGET:
        return False
    progress = journal.get("targetProgress", {}).get(PERSISTENCE_IMAGE_UPDATE_TARGET, {})
    return isinstance(progress, dict) and (
        progress.get("applyIntent") is True
        or isinstance(progress.get("candidateContainerId"), str)
        or progress.get("phase") == "APPLIED"
    )


def _rollback_known_mutations(
    *,
    plan: dict[str, Any],
    active_compose: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> None:
    """Rollback only exact, recorded candidate containers; never restore SQLite bytes."""

    if _persistence_image_update_may_have_started(plan, journal):
        journal["lastErrorCode"] = "persistence_update_requires_manual_reconciliation"
        _write_journal(
            journal_path,
            journal,
            "RECOVERY_REQUIRED",
            state="RECOVERY_REQUIRED",
        )
        raise StageTransitionError(
            "Persistence image replacement may have started; preserve the current database and require read-only reconciliation"
        )

    active_lock = runtime_update._load_lock(Path(plan["activeInputs"]["imageLock"]["path"]))
    active_images = _lock_image_ids(active_lock)
    for service_name in reversed(plan["targets"]):
        progress = journal["targetProgress"][service_name]
        if service_name == "account-center" and _account_center_image_downgrade_may_be_unsafe(
            plan, progress
        ):
            journal["lastErrorCode"] = ACCOUNT_CENTER_DOWNGRADE_BLOCKED_ERROR
            _write_journal(
                journal_path,
                journal,
                "ACCOUNT_CENTER_SCHEMA_DOWNGRADE_BLOCKED",
                state="RECOVERY_REQUIRED",
            )
            raise StageTransitionError(
                "Account Center may have migrated durable SQLite; preserve its candidate image and require roll-forward recovery"
            )
        candidate_id = progress.get("candidateContainerId")
        if not isinstance(candidate_id, str):
            if progress.get("applyIntent") is True:
                raise StageTransitionError(
                    "target apply outcome is unresolved; refusing rollback mutation"
                )
            continue
        expected_candidate = plan["candidateServices"][service_name]["candidate"]
        candidate_service = _runtime_service_inventory(
            preflight._compose_config(
                docker_prefix,
                [Path(row["path"]) for row in plan["candidateInputs"]["composeFiles"]],
                [Path(row["path"]) for row in plan["candidateInputs"]["envFiles"]],
            )
        ).get(service_name)
        if not isinstance(candidate_service, dict):
            raise StageTransitionError(
                f"candidate service {service_name} disappeared from its plan"
            )
        if progress["added"]:
            if not _container_exists(candidate_id, docker_prefix):
                progress["phase"] = "ROLLED_BACK"
                _write_journal(
                    journal_path,
                    journal,
                    f"{service_name.upper().replace('-', '_')}_ABSENT_READBACK",
                )
                continue
            current = runtime_update._checked_stdout(
                [*docker_prefix, "inspect", candidate_id],
                label="operation-owned provider container inspection",
            )
            inspected = runtime_update._decode_json_output(current, "created service inspection")
            if (
                not isinstance(inspected, list)
                or len(inspected) != 1
                or not isinstance(inspected[0], dict)
            ):
                raise StageTransitionError("created service ID inspection is ambiguous")
            row = inspected[0]
            config_row = row.get("Config")
            labels = config_row.get("Labels") if isinstance(config_row, dict) else None
            if (
                row.get("Id") != candidate_id
                or not isinstance(labels, dict)
                or labels.get("com.docker.compose.project") != plan["project"]
                or labels.get("com.docker.compose.service") != service_name
                or row.get("Image") != expected_candidate["imageId"]
                or config_row.get("Image") != expected_candidate["imageRef"]
            ):
                raise StageTransitionError(
                    "refusing to remove a container not owned by this exact operation"
                )
            progress["rollbackIntent"] = True
            progress["rollbackContainerId"] = candidate_id
            _write_journal(
                journal_path, journal, f"{service_name.upper().replace('-', '_')}_REMOVE_REQUESTED"
            )
            try:
                result = runtime_update._run_command(
                    [*docker_prefix, "rm", "-f", candidate_id],
                    timeout_seconds=60,
                    external_mutation=True,
                )
            except runtime_update.RuntimeUpdateOutcomeUnknown as error:
                raise StageTransitionError("owned-service removal outcome is unknown") from error
            if result.returncode != 0:
                raise StageTransitionError("owned-service removal returned nonzero")
            if _container_exists(candidate_id, docker_prefix):
                raise StageTransitionError(
                    "operation-owned provider container remained after rollback"
                )
            progress["phase"] = "ROLLED_BACK"
            progress["rollbackIntent"] = False
            _write_journal(
                journal_path, journal, f"{service_name.upper().replace('-', '_')}_REMOVED"
            )
            continue

        active_service = active_compose.get("services", {}).get(service_name)
        active_image = _service_image(active_compose, active_images, service_name)
        if not isinstance(active_service, dict):
            raise StageTransitionError(f"old service {service_name} is missing from active Compose")
        progress["rollbackIntent"] = True
        _write_journal(
            journal_path, journal, f"{service_name.upper().replace('-', '_')}_ROLLBACK_REQUESTED"
        )
        try:
            result = _compose_target_update(
                plan=plan,
                service=service_name,
                use_candidate=False,
                docker_prefix=docker_prefix,
            )
        except runtime_update.RuntimeUpdateOutcomeUnknown as error:
            raise StageTransitionError(f"{service_name} rollback outcome is unknown") from error
        if result.returncode != 0:
            raise StageTransitionError(f"{service_name} rollback returned nonzero")
        rolled_back = _wait_for_target_service(
            project=plan["project"],
            service_name=service_name,
            expected_image=active_image,
            has_healthcheck=bool(active_service.get("healthcheck")),
            timeout_seconds=plan["policy"]["healthTimeoutSeconds"],
            docker_prefix=docker_prefix,
        )
        if rolled_back["containerId"] == candidate_id:
            raise StageTransitionError(f"{service_name} rollback retained the candidate container")
        progress["rollbackIntent"] = False
        progress["rollbackContainerId"] = rolled_back["containerId"]
        progress["phase"] = "ROLLED_BACK"
        _write_journal(
            journal_path, journal, f"{service_name.upper().replace('-', '_')}_ROLLED_BACK"
        )

    active_services = _live_service_inventory(
        compose=active_compose,
        image_lock=active_lock,
        docker_prefix=docker_prefix,
        mount_identity_by_service=plan["runtimeConfigIdentity"]["active"],
    )
    runtime_update._verify_live_network_membership(active_compose, active_services, docker_prefix)
    if "listenerRepairState" in plan:
        listener_state = _validate_listener_repair_state_record(
            plan.get("listenerRepairState"), plan["activeServices"]
        )
        for service_name, row in listener_state["services"].items():
            current = active_services.get(service_name)
            if not isinstance(current, dict):
                raise StageTransitionError(
                    f"rolled-back {service_name} provider container is missing"
                )
            command = _service_command_readback(
                container_id=current["containerId"], docker_prefix=docker_prefix
            )
            if command != row["activeCommand"]:
                raise StageTransitionError(
                    f"rolled-back {service_name} provider listener command is not restored"
                )
            if (
                _service_environment_sha256(
                    container_id=current["containerId"], docker_prefix=docker_prefix
                )
                != row["environmentSha256"]
            ):
                raise StageTransitionError(
                    f"rolled-back {service_name} provider environment is not restored"
                )
        _verify_cloud_runner_idle(
            plan=plan,
            active_compose=active_compose,
            candidate_compose=active_compose,
            docker_prefix=docker_prefix,
        )
    _resume_old_executor_if_needed(
        plan=plan,
        journal=journal,
        docker_prefix=docker_prefix,
        require_original_instance=plan.get("imageUpdateTarget") in PRESERVE_NAVIGATOR_IMAGE_TARGETS
        or "listenerRepairState" in plan,
    )
    _resume_ingress_after_safe_state(
        journal=journal,
        journal_path=journal_path,
        docker_prefix=docker_prefix,
    )
    _write_journal(journal_path, journal, "FAILED_ROLLED_BACK", state="FAILED_ROLLED_BACK")


def apply_plan_file(
    plan_path: Path,
    *,
    expected_digest: str,
) -> dict[str, Any]:
    """Apply the exact confirmed plan with drain, backup, ordered health checks, and safe recovery."""

    runtime_update._require_update_operator()
    if (
        plan_path.is_symlink()
        or not plan_path.is_file()
        or not preflight._private_artifact(plan_path)
    ):
        raise StageTransitionError("stage transition plan must be a private artifact file")
    metadata = plan_path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise StageTransitionError("stage transition plan must be an owner-only mode 0600 file")
    plan = _read_json(plan_path, "stage transition plan")
    _validate_plan(plan)
    if expected_digest != plan["planSha256"]:
        raise StageTransitionError("apply confirmation does not match the immutable plan digest")
    journal_dir = Path(plan["journalDir"])
    runtime_update._ensure_journal_directory(journal_dir, create=True)
    journal_path = _journal_path(journal_dir, plan["operationId"])
    docker_prefix = plan["policy"]["dockerPrefix"]
    runtime_update._validate_docker_prefix(docker_prefix)
    with runtime_update._operation_lock(plan["project"]):
        if journal_path.exists() or journal_path.is_symlink():
            raise StageTransitionError("operation journal already exists; apply is never replayed")
        _verify_all_inputs(plan)
        _check_selection_unchanged(plan)
        _candidate_preflight(plan)
        active_compose, _active_lock = _verify_active_baseline(plan, docker_prefix)
        candidate_compose, _candidate_lock = _candidate_configs(plan, docker_prefix)
        account_center_update_state = plan.get("accountCenterImageUpdateState")
        if plan.get("imageUpdateTarget") == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET:
            targets, candidate_services = _validate_phase_topology(
                phase=plan["phase"],
                image_update_target=ACCOUNT_CENTER_IMAGE_UPDATE_TARGET,
                active=active_compose,
                candidate=candidate_compose,
                active_lock=_active_lock,
                candidate_lock=_candidate_lock,
            )
            if targets != plan["targets"] or candidate_services != plan["candidateServices"]:
                raise StageTransitionError(
                    "Account Center image update topology changed after the plan was sealed"
                )
            if not isinstance(account_center_update_state, dict):
                raise StageTransitionError("Account Center image update baseline is missing")
            account_center = plan["activeServices"].get(ACCOUNT_CENTER_IMAGE_UPDATE_TARGET)
            if not isinstance(account_center, dict):
                raise StageTransitionError("active Account Center service identity is missing")
            database_state = _account_center_database_state(
                compose=active_compose,
                project=plan["project"],
                container_id=account_center["containerId"],
                image_ref=account_center["imageRef"],
                image_id=account_center["imageId"],
                docker_prefix=docker_prefix,
                include_startup_recovery_counts=True,
            )
            capacity_state = _memory_update_actual_capacity(
                compose=active_compose,
                services=plan["activeServices"],
                max_memory_mib=3584,
                max_cpus=1.95,
                docker_prefix=docker_prefix,
            )
            if not _account_center_database_state_matches_baseline(
                account_center_update_state.get("database"), database_state
            ) or capacity_state != account_center_update_state.get("runtimeCapacity"):
                raise StageTransitionError(
                    "Account Center database/profile or 15-service resource baseline changed after planning"
                )
        if (
            plan.get("imageUpdateTarget") == EXCHANGE_IMAGE_UPDATE_TARGET
            and plan.get("phase") == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
        ):
            targets, candidate_services = _validate_phase_topology(
                phase=plan["phase"],
                image_update_target=EXCHANGE_IMAGE_UPDATE_TARGET,
                active=active_compose,
                candidate=candidate_compose,
                active_lock=_active_lock,
                candidate_lock=_candidate_lock,
            )
            if targets != plan["targets"] or candidate_services != plan["candidateServices"]:
                raise StageTransitionError(
                    "Exchange Graph-bootstrap image update topology changed after the plan was sealed"
                )
        if plan.get("memoryUpdateTarget") is not None:
            targets, candidate_services = _validate_phase_topology(
                phase=plan["phase"],
                memory_update_target=plan["memoryUpdateTarget"],
                active=active_compose,
                candidate=candidate_compose,
                active_lock=_active_lock,
                candidate_lock=_candidate_lock,
            )
            if targets != plan["targets"] or candidate_services != plan["candidateServices"]:
                raise StageTransitionError(
                    "memory update topology changed after the plan was sealed"
                )
            active_memory = _memory_update_limit_bytes(
                _runtime_service_inventory(active_compose)[plan["memoryUpdateTarget"]],
                "active memory update service",
            )
            candidate_memory = _memory_update_limit_bytes(
                _runtime_service_inventory(candidate_compose)[plan["memoryUpdateTarget"]],
                "candidate memory update service",
            )
            memory_state = plan["memoryUpdateState"]
            active_hash = _compose_service_config_hash(
                service_name=plan["memoryUpdateTarget"],
                compose_files=[Path(row["path"]) for row in plan["activeInputs"]["composeFiles"]],
                env_files=[Path(row["path"]) for row in plan["activeInputs"]["envFiles"]],
                docker_prefix=docker_prefix,
            )
            candidate_hash = _compose_service_config_hash(
                service_name=plan["memoryUpdateTarget"],
                compose_files=[
                    Path(row["path"]) for row in plan["candidateInputs"]["composeFiles"]
                ],
                env_files=[Path(row["path"]) for row in plan["candidateInputs"]["envFiles"]],
                docker_prefix=docker_prefix,
            )
            if (
                active_memory != memory_state["activeMemoryBytes"]
                or candidate_memory != memory_state["candidateMemoryBytes"]
                or active_hash != memory_state["activeComposeConfigHash"]
                or candidate_hash != memory_state["candidateComposeConfigHash"]
            ):
                raise StageTransitionError(
                    "memory limits or Compose hash changed after the plan was sealed"
                )
        journal = _make_journal(plan, plan_path)
        _create_initial_journal(journal_path, journal)
        if plan.get("memoryUpdateTarget") is not None:
            return _apply_memory_update(
                plan=plan,
                active_compose=active_compose,
                candidate_compose=candidate_compose,
                candidate_lock=_candidate_lock,
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker_prefix,
            )
        try:
            runtime_update._pause_edge(
                plan=plan,
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker_prefix,
            )
            runtime_update._drain_navigator(
                plan=plan,
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker_prefix,
            )
            _atomic_sqlite_snapshot(
                plan=plan,
                docker_prefix=docker_prefix,
                journal=journal,
                journal_path=journal_path,
            )
            preexisting_schema = _validate_bootstrap_persistence_baseline(
                plan, journal["database"]["snapshot"]
            )
            if preexisting_schema is not None:
                _write_journal(
                    journal_path,
                    journal,
                    "PERSISTENCE_CANDIDATE_SCHEMA_VERIFIED",
                )
            if journal["cloudRunnerIdle"]["required"]:
                before_target = _verify_cloud_runner_idle(
                    plan=plan,
                    active_compose=active_compose,
                    candidate_compose=candidate_compose,
                    docker_prefix=docker_prefix,
                )
                journal["cloudRunnerIdle"]["beforeTarget"] = before_target
                _write_journal(
                    journal_path,
                    journal,
                    "CLOUD_RUNNER_IDLE_BEFORE_TARGET_VERIFIED",
                )
            if journal["listenerRepair"]["required"]:
                issued_state = _verify_listener_repair_active_state(
                    plan=plan,
                    active_compose=active_compose,
                    docker_prefix=docker_prefix,
                )
                journal["listenerRepair"]["issuedStateBeforeTarget"] = {
                    field: issued_state[field]
                    for field in (
                        "status",
                        "issuedStateSha256",
                        "profileCount",
                        "connectionCount",
                        "bindingCount",
                        "verifiedProviderIds",
                        "catalogStatus",
                    )
                }
                _write_journal(
                    journal_path,
                    journal,
                    "LISTENER_REPAIR_ISSUED_STATE_VERIFIED",
                )
            for service_name in plan["targets"]:
                if (
                    (
                        plan.get("imageUpdateTarget") == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
                        and plan.get("phase") in ACCOUNT_CENTER_IMAGE_UPDATE_PHASES
                    )
                    or (
                        plan.get("imageUpdateTarget")
                        in {
                            HUGGINGFACE_IMAGE_UPDATE_TARGET,
                            CLOUD_IMAGE_UPDATE_TARGET,
                            GITHUB_ISSUES_IMAGE_UPDATE_TARGET,
                        }
                        and plan.get("phase") == "issued-connector-activation"
                    )
                    or (
                        plan.get("imageUpdateTarget") == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET
                        and plan.get("phase") in WORKSPACE_AUTHORITY_IMAGE_UPDATE_PHASES
                    )
                    or (
                        plan.get("imageUpdateTarget") == EXCHANGE_IMAGE_UPDATE_TARGET
                        and plan.get("phase") == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
                    )
                ) and service_name == plan.get("imageUpdateTarget"):
                    if journal["exchangeReadiness"]["required"] is not True:
                        raise StageTransitionError(
                            "provider image update requires Exchange live readiness"
                        )
                    _record_exchange_readiness(
                        plan=plan,
                        compose=candidate_compose,
                        journal=journal,
                        journal_path=journal_path,
                        docker_prefix=docker_prefix,
                    )
                    update_key = {
                        ACCOUNT_CENTER_IMAGE_UPDATE_TARGET: "accountCenterImageUpdate",
                        HUGGINGFACE_IMAGE_UPDATE_TARGET: "huggingfaceImageUpdate",
                        CLOUD_IMAGE_UPDATE_TARGET: "cloudImageUpdate",
                        GITHUB_ISSUES_IMAGE_UPDATE_TARGET: "githubIssuesImageUpdate",
                        WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET: "workspaceAuthorityImageUpdate",
                        EXCHANGE_IMAGE_UPDATE_TARGET: "exchangeImageUpdate",
                    }[plan["imageUpdateTarget"]]
                    update_state = journal[update_key]
                    update_state["beforeReadiness"] = _image_update_exchange_readiness_checkpoint(
                        plan, journal, candidate_compose=candidate_compose
                    )
                    _write_journal(
                        journal_path,
                        journal,
                        "PROVIDER_EXCHANGE_READINESS_BEFORE_UPDATE_VERIFIED",
                    )
                    if plan.get("imageUpdateTarget") not in {
                        WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET,
                        EXCHANGE_IMAGE_UPDATE_TARGET,
                    }:
                        _verify_provider_monitor_quiescence(
                            plan=plan,
                            candidate_compose=candidate_compose,
                            journal=journal,
                            journal_path=journal_path,
                            docker_prefix=docker_prefix,
                        )
                if (
                    plan.get("imageUpdateTarget") == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
                    and service_name == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
                ):
                    account_center_state = journal["accountCenterImageUpdate"]
                    baseline = plan["accountCenterImageUpdateState"]
                    account_center = plan["activeServices"][ACCOUNT_CENTER_IMAGE_UPDATE_TARGET]
                    database_state = _account_center_database_state(
                        compose=active_compose,
                        project=plan["project"],
                        container_id=account_center["containerId"],
                        image_ref=account_center["imageRef"],
                        image_id=account_center["imageId"],
                        docker_prefix=docker_prefix,
                        include_startup_recovery_counts=True,
                    )
                    capacity_state = _memory_update_actual_capacity(
                        compose=active_compose,
                        services=plan["activeServices"],
                        max_memory_mib=3584,
                        max_cpus=1.95,
                        docker_prefix=docker_prefix,
                    )
                    if (
                        not _account_center_database_state_matches_baseline(
                            baseline["database"], database_state
                        )
                        or capacity_state != baseline["runtimeCapacity"]
                    ):
                        raise StageTransitionError(
                            "Account Center database/profile or 15-service budget changed before target mutation"
                        )
                    account_center_state["beforeDatabaseIdentity"] = database_state
                    account_center_state["beforeRuntimeCapacity"] = capacity_state
                    account_center_state["identityReadbackStatus"] = "PRE_MUTATION_VERIFIED"
                    _write_journal(
                        journal_path,
                        journal,
                        "ACCOUNT_CENTER_PRE_MUTATION_IDENTITY_VERIFIED",
                    )
                if (
                    plan.get("imageUpdateTarget") == PERSISTENCE_IMAGE_UPDATE_TARGET
                    and service_name == PERSISTENCE_IMAGE_UPDATE_TARGET
                ):
                    if (
                        journal["exchangeReadiness"]["required"]
                        and journal["exchangeReadiness"]["verified"] is not True
                    ):
                        _record_exchange_readiness(
                            plan=plan,
                            compose=candidate_compose,
                            journal=journal,
                            journal_path=journal_path,
                            docker_prefix=docker_prefix,
                        )
                    _verify_persistence_update_quiescence(
                        plan=plan,
                        journal=journal,
                        journal_path=journal_path,
                        docker_prefix=docker_prefix,
                    )
                if (
                    service_name == "navigator"
                    and journal["exchangeReadiness"]["required"]
                    and journal["exchangeReadiness"]["verified"] is not True
                ):
                    _record_exchange_readiness(
                        plan=plan,
                        compose=candidate_compose,
                        journal=journal,
                        journal_path=journal_path,
                        docker_prefix=docker_prefix,
                    )
                _update_target(
                    plan=plan,
                    candidate_compose=candidate_compose,
                    journal=journal,
                    journal_path=journal_path,
                    docker_prefix=docker_prefix,
                    service_name=service_name,
                )
                if (
                    (
                        plan.get("imageUpdateTarget") == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
                        and plan.get("phase") in ACCOUNT_CENTER_IMAGE_UPDATE_PHASES
                    )
                    or (
                        plan.get("imageUpdateTarget")
                        in {
                            HUGGINGFACE_IMAGE_UPDATE_TARGET,
                            CLOUD_IMAGE_UPDATE_TARGET,
                            GITHUB_ISSUES_IMAGE_UPDATE_TARGET,
                        }
                        and plan.get("phase") == "issued-connector-activation"
                    )
                    or (
                        plan.get("imageUpdateTarget") == WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET
                        and plan.get("phase") in WORKSPACE_AUTHORITY_IMAGE_UPDATE_PHASES
                    )
                    or (
                        plan.get("imageUpdateTarget") == EXCHANGE_IMAGE_UPDATE_TARGET
                        and plan.get("phase") == OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
                    )
                ) and service_name == plan.get("imageUpdateTarget"):
                    _record_exchange_readiness(
                        plan=plan,
                        compose=candidate_compose,
                        journal=journal,
                        journal_path=journal_path,
                        docker_prefix=docker_prefix,
                    )
                    update_key = {
                        ACCOUNT_CENTER_IMAGE_UPDATE_TARGET: "accountCenterImageUpdate",
                        HUGGINGFACE_IMAGE_UPDATE_TARGET: "huggingfaceImageUpdate",
                        CLOUD_IMAGE_UPDATE_TARGET: "cloudImageUpdate",
                        GITHUB_ISSUES_IMAGE_UPDATE_TARGET: "githubIssuesImageUpdate",
                        WORKSPACE_AUTHORITY_IMAGE_UPDATE_TARGET: "workspaceAuthorityImageUpdate",
                        EXCHANGE_IMAGE_UPDATE_TARGET: "exchangeImageUpdate",
                    }[plan["imageUpdateTarget"]]
                    journal[update_key]["afterReadiness"] = (
                        _image_update_exchange_readiness_checkpoint(
                            plan, journal, candidate_compose=candidate_compose
                        )
                    )
                    if update_key == "accountCenterImageUpdate":
                        _verify_provider_monitor_quiescence(
                            plan=plan,
                            candidate_compose=candidate_compose,
                            journal=journal,
                            journal_path=journal_path,
                            docker_prefix=docker_prefix,
                            checkpoint="after",
                        )
                    _write_journal(
                        journal_path,
                        journal,
                        "PROVIDER_EXCHANGE_READINESS_AFTER_UPDATE_VERIFIED",
                    )
                if (
                    plan.get("imageUpdateTarget") in IMAGE_UPDATE_READINESS_TARGETS
                    and service_name == plan.get("imageUpdateTarget")
                    and journal["exchangeReadiness"]["required"]
                    and journal["exchangeReadiness"]["verified"] is not True
                ):
                    _record_exchange_readiness(
                        plan=plan,
                        compose=candidate_compose,
                        journal=journal,
                        journal_path=journal_path,
                        docker_prefix=docker_prefix,
                    )
                if (
                    journal["cloudRunnerIdle"]["required"] or journal["listenerRepair"]["required"]
                ) and service_name == CLOUD_IMAGE_UPDATE_TARGET:
                    try:
                        after_target = _verify_cloud_runner_idle(
                            plan=plan,
                            active_compose=candidate_compose,
                            candidate_compose=candidate_compose,
                            docker_prefix=docker_prefix,
                        )
                    except StageTransitionError as error:
                        journal["cloudRunnerIdle"]["postTargetReadbackFailed"] = True
                        journal["lastErrorCode"] = "cloud_runner_idle_post_target_unverified"
                        _write_journal(
                            journal_path,
                            journal,
                            "RECOVERY_REQUIRED",
                            state="RECOVERY_REQUIRED",
                        )
                        raise StageTransitionError(
                            "provider target changed but Exchange background work is no longer verifiably idle; keep ingress held for read-only recovery"
                        ) from error
                    journal["cloudRunnerIdle"]["afterTarget"] = after_target
                    _write_journal(
                        journal_path,
                        journal,
                        "CLOUD_RUNNER_IDLE_AFTER_TARGET_VERIFIED",
                    )
                if service_name == "navigator-persistence":
                    try:
                        if plan.get("imageUpdateTarget") == PERSISTENCE_IMAGE_UPDATE_TARGET:
                            _validate_persistence_same_schema_update(
                                plan=plan,
                                journal=journal,
                                journal_path=journal_path,
                                docker_prefix=docker_prefix,
                            )
                        else:
                            _validate_database_migration(
                                plan=plan,
                                journal=journal,
                                journal_path=journal_path,
                                docker_prefix=docker_prefix,
                            )
                    except (
                        OSError,
                        StageTransitionError,
                        runtime_update.RuntimeUpdateError,
                        subprocess.SubprocessError,
                    ) as error:
                        journal["database"]["migrationGateFailed"] = True
                        journal["lastErrorCode"] = "database_migration_contract_failed"
                        _write_journal(
                            journal_path,
                            journal,
                            "RECOVERY_REQUIRED",
                            state="RECOVERY_REQUIRED",
                        )
                        raise StageTransitionError(
                            "Persistence migration contract failed; keep ingress held and do not roll back the database image or restore snapshot bytes"
                        ) from error
            if journal["listenerRepair"]["required"]:
                _verify_listener_repair_target_commands(
                    plan=plan,
                    journal=journal,
                    docker_prefix=docker_prefix,
                )
                journal["listenerRepair"]["targetCommandsVerified"] = True
                _write_journal(
                    journal_path,
                    journal,
                    "LISTENER_REPAIR_COMMANDS_VERIFIED",
                )
                _record_exchange_readiness(
                    plan=plan,
                    compose=candidate_compose,
                    journal=journal,
                    journal_path=journal_path,
                    docker_prefix=docker_prefix,
                )
            candidate_lock = runtime_update._load_lock(
                Path(plan["candidateInputs"]["imageLock"]["path"])
            )
            candidate_services = _live_service_inventory(
                compose=candidate_compose,
                image_lock=candidate_lock,
                docker_prefix=docker_prefix,
                mount_identity_by_service=plan["runtimeConfigIdentity"]["candidate"],
            )
            for service_name, old in plan["activeServices"].items():
                if (
                    service_name not in set(plan["targets"])
                    and candidate_services.get(service_name) != old
                ):
                    raise StageTransitionError(
                        f"untargeted service {service_name} changed during transition"
                    )
            runtime_update._verify_live_network_membership(
                candidate_compose, candidate_services, docker_prefix
            )
            if plan.get("imageUpdateTarget") == ACCOUNT_CENTER_IMAGE_UPDATE_TARGET:
                account_center_state = journal["accountCenterImageUpdate"]
                candidate_account_center = candidate_services.get(
                    ACCOUNT_CENTER_IMAGE_UPDATE_TARGET
                )
                try:
                    capacity_state = _memory_update_actual_capacity(
                        compose=candidate_compose,
                        services=candidate_services,
                        max_memory_mib=3584,
                        max_cpus=1.95,
                        docker_prefix=docker_prefix,
                    )
                    if not isinstance(candidate_account_center, dict):
                        raise StageTransitionError(
                            "candidate Account Center service is missing from live inventory"
                        )
                    database_state = _account_center_database_state(
                        compose=candidate_compose,
                        project=plan["project"],
                        container_id=candidate_account_center["containerId"],
                        image_ref=candidate_account_center["imageRef"],
                        image_id=candidate_account_center["imageId"],
                        docker_prefix=docker_prefix,
                        include_startup_recovery_counts=True,
                    )
                except StageTransitionError as error:
                    account_center_state["identityReadbackStatus"] = "UNREADABLE"
                    journal["lastErrorCode"] = "account_center_image_update_readback_failed"
                    _write_journal(
                        journal_path,
                        journal,
                        "RECOVERY_REQUIRED",
                        state="RECOVERY_REQUIRED",
                    )
                    raise StageTransitionError(
                        "Account Center database/resource read-back failed; preserve the candidate image for read-only recovery"
                    ) from error
                account_center_state["afterDatabaseIdentity"] = database_state
                account_center_state["afterRuntimeCapacity"] = capacity_state
                verified = (
                    _account_center_database_state_matches_baseline(
                        plan["accountCenterImageUpdateState"]["database"], database_state
                    )
                    and capacity_state == plan["accountCenterImageUpdateState"]["runtimeCapacity"]
                )
                account_center_state["identityReadbackStatus"] = (
                    "VERIFIED" if verified else "MISMATCH"
                )
                if not verified:
                    journal["lastErrorCode"] = "account_center_image_update_readback_mismatch"
                    _write_journal(
                        journal_path,
                        journal,
                        "RECOVERY_REQUIRED",
                        state="RECOVERY_REQUIRED",
                    )
                    raise StageTransitionError(
                        "Account Center schema/profile identity or actual resource limits changed; preserve the candidate image for read-only recovery"
                    )
                _write_journal(
                    journal_path,
                    journal,
                    "ACCOUNT_CENTER_DATABASE_AND_RESOURCE_READBACK_VERIFIED",
                )
            if (
                journal["exchangeReadiness"]["required"]
                and journal["exchangeReadiness"]["verified"] is not True
            ):
                raise StageTransitionError(
                    "transition reached cutover without required live Exchange readiness"
                )
            _resume_executor_after_transition(
                plan=plan,
                journal=journal,
                candidate_compose=candidate_compose,
                docker_prefix=docker_prefix,
            )
            resource_readback = _validate_post_update_capacity(
                compose=candidate_compose,
                services=candidate_services,
                docker_prefix=docker_prefix,
                minimum_available_mib=plan["candidatePreflight"]["minimumMemAvailableMiB"],
            )
            journal["resourceReadback"] = resource_readback
            _write_journal(journal_path, journal, "RESOURCE_READBACK_VERIFIED")
            selection = _promote_selection(
                plan=plan,
                journal=journal,
                journal_path=journal_path,
            )
            _resume_ingress_after_safe_state(
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker_prefix,
            )
            delivery_readback = _verify_final_delivery_readback(
                journal=journal,
                docker_prefix=docker_prefix,
            )
            journal["edge"]["latestDeadCount"] = delivery_readback["dead_count"]
            _write_journal(journal_path, journal, "APPLIED", state="APPLIED")
            return {
                **_journal_summary(journal),
                "activeRuntimeSelection": _selection_summary(selection),
            }
        except runtime_update.RuntimeUpdateOutcomeUnknown as error:
            journal["lastErrorCode"] = "docker_mutation_outcome_unknown"
            _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
            raise StageTransitionError(
                "Docker mutation outcome is unknown; the ingress hold remains and no mutation was replayed"
            ) from error
        except StageTransitionError as error:
            if journal.get("state") == "RECOVERY_REQUIRED" or any(
                row.get("phase") in {"APPLY_OUTCOME_UNKNOWN", "APPLY_READBACK_UNKNOWN"}
                for row in journal.get("targetProgress", {}).values()
                if isinstance(row, dict)
            ):
                raise
            if (
                journal.get("selection", {}).get("promoted") is True
                or journal.get("edge", {}).get("resumeRevision") is not None
            ):
                journal["lastErrorCode"] = "post_cutover_readback_failed"
                _write_journal(
                    journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED"
                )
                raise StageTransitionError(
                    "post-cutover ingress readback failed; keep the recorded delivery state for manual recovery"
                ) from error
            if _selection_promotion_uncertain(journal):
                journal["lastErrorCode"] = "runtime_selection_promotion_outcome_unknown"
                _write_journal(
                    journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED"
                )
                raise StageTransitionError(
                    "runtime selector promotion may have committed; ingress remains held for read-only recovery"
                ) from error
            if _persistence_image_update_may_have_started(plan, journal):
                journal["lastErrorCode"] = "persistence_update_requires_manual_reconciliation"
                _write_journal(
                    journal_path,
                    journal,
                    "RECOVERY_REQUIRED",
                    state="RECOVERY_REQUIRED",
                )
                raise StageTransitionError(
                    "Persistence image replacement may have started; no automatic image rollback was attempted"
                ) from error
            try:
                _rollback_known_mutations(
                    plan=plan,
                    active_compose=active_compose,
                    journal=journal,
                    journal_path=journal_path,
                    docker_prefix=docker_prefix,
                )
            except (
                OSError,
                StageTransitionError,
                runtime_update.RuntimeUpdateError,
                subprocess.SubprocessError,
            ) as rollback_error:
                if journal.get("lastErrorCode") != ACCOUNT_CENTER_DOWNGRADE_BLOCKED_ERROR:
                    journal["lastErrorCode"] = "safe_rollback_not_verified"
                _write_journal(
                    journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED"
                )
                raise StageTransitionError(
                    "stage transition failed and rollback was not fully verified; ingress remains held"
                ) from rollback_error
            journal["lastErrorCode"] = "candidate_health_or_validation_failed"
            _write_journal(journal_path, journal, "FAILED_ROLLED_BACK", state="FAILED_ROLLED_BACK")
            raise StageTransitionError(
                "stage transition failed; old service images/config were restored without restoring SQLite"
            ) from error
        except (
            OSError,
            runtime_update.RuntimeUpdateError,
            preflight.PreflightError,
            subprocess.SubprocessError,
        ) as error:
            if journal.get("state") == "RECOVERY_REQUIRED":
                raise StageTransitionError(
                    "stage transition requires read-only recovery; no uncertain mutation was replayed"
                ) from error
            if (
                journal.get("selection", {}).get("promoted") is True
                or journal.get("edge", {}).get("resumeRevision") is not None
            ):
                journal["lastErrorCode"] = "post_cutover_readback_failed"
                _write_journal(
                    journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED"
                )
                raise StageTransitionError(
                    "post-cutover ingress readback failed; keep the recorded delivery state for manual recovery"
                ) from error
            if _selection_promotion_uncertain(journal):
                journal["lastErrorCode"] = "runtime_selection_promotion_outcome_unknown"
                _write_journal(
                    journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED"
                )
                raise StageTransitionError(
                    "runtime selector promotion may have committed; ingress remains held for read-only recovery"
                ) from error
            if _persistence_image_update_may_have_started(plan, journal):
                journal["lastErrorCode"] = "persistence_update_requires_manual_reconciliation"
                _write_journal(
                    journal_path,
                    journal,
                    "RECOVERY_REQUIRED",
                    state="RECOVERY_REQUIRED",
                )
                raise StageTransitionError(
                    "Persistence image replacement may have started; no automatic image rollback was attempted"
                ) from error
            try:
                _rollback_known_mutations(
                    plan=plan,
                    active_compose=active_compose,
                    journal=journal,
                    journal_path=journal_path,
                    docker_prefix=docker_prefix,
                )
            except (
                OSError,
                StageTransitionError,
                runtime_update.RuntimeUpdateError,
                subprocess.SubprocessError,
            ) as rollback_error:
                if journal.get("lastErrorCode") != ACCOUNT_CENTER_DOWNGRADE_BLOCKED_ERROR:
                    journal["lastErrorCode"] = "safe_rollback_not_verified"
                _write_journal(
                    journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED"
                )
                raise StageTransitionError(
                    "stage transition failed and rollback was not fully verified; ingress remains held"
                ) from rollback_error
            journal["lastErrorCode"] = "candidate_health_or_validation_failed"
            _write_journal(journal_path, journal, "FAILED_ROLLED_BACK", state="FAILED_ROLLED_BACK")
            raise StageTransitionError(
                "stage transition failed; old service images/config were restored without restoring SQLite"
            ) from error


def operation_status(journal_dir: Path, operation_id: str) -> dict[str, Any]:
    """Read one durable transition journal without contacting Docker or application APIs."""

    runtime_update._require_update_operator()
    runtime_update._ensure_journal_directory(journal_dir, create=False)
    return _journal_summary(_read_journal(_journal_path(journal_dir, operation_id)))


def resume_operation(operation_id: str, *, journal_dir: Path) -> dict[str, Any]:
    """Perform read-only recovery reconciliation; never repeat a Compose mutation."""

    runtime_update._require_update_operator()
    runtime_update._ensure_journal_directory(journal_dir, create=False)
    journal_path = _journal_path(journal_dir, operation_id)
    journal = _read_journal(journal_path)
    if journal.get("state") in {"APPLIED", "FAILED_ROLLED_BACK", "RECOVERY_REQUIRED"}:
        return _journal_summary(journal)
    plan_path = Path(journal.get("planPath", ""))
    plan = _read_json(plan_path, "stage transition plan")
    _validate_plan(plan)
    if plan.get("operationId") != journal.get("operationId") or plan.get(
        "planSha256"
    ) != journal.get("planSha256"):
        raise StageTransitionError("journal no longer matches its exact private plan")
    with runtime_update._operation_lock(plan["project"]):
        progress = journal.get("targetProgress")
        if not isinstance(progress, dict):
            raise StageTransitionError("stage transition target journal is malformed")
        unresolved = [
            name
            for name, row in progress.items()
            if isinstance(row, dict)
            and row.get("applyIntent") is True
            and row.get("phase") not in {"APPLIED", "ROLLED_BACK"}
        ]
        if unresolved or journal.get("selection", {}).get("promotionIntent") is True:
            journal["lastErrorCode"] = "docker_or_selection_outcome_unresolved"
            _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
            return _journal_summary(journal)
        # Reconciliation can report observed state, but it never issues another Docker command.
        try:
            _verify_all_inputs(plan)
            candidate_compose, candidate_lock = _candidate_configs(
                plan, plan["policy"]["dockerPrefix"]
            )
            candidate_state = _live_service_inventory(
                compose=candidate_compose,
                image_lock=candidate_lock,
                docker_prefix=plan["policy"]["dockerPrefix"],
                mount_identity_by_service=plan["runtimeConfigIdentity"]["candidate"],
            )
            expected_ids = {
                name: row.get("healthyContainerId")
                for name, row in progress.items()
                if isinstance(row, dict) and row.get("phase") == "APPLIED"
            }
            if any(
                not isinstance(expected_id, str)
                or candidate_state.get(name, {}).get("containerId") != expected_id
                for name, expected_id in expected_ids.items()
            ):
                raise StageTransitionError("recorded target container identity changed")
            selection = _current_selection_digest(Path(journal["selection"]["path"]))
            if journal["selection"].get("promoted") is True and selection != journal[
                "selection"
            ].get("newSha256"):
                raise StageTransitionError(
                    "promoted runtime selection no longer matches its journal"
                )
        except (
            OSError,
            StageTransitionError,
            runtime_update.RuntimeUpdateError,
            subprocess.SubprocessError,
        ):
            journal["lastErrorCode"] = "read_only_reconciliation_mismatch"
            _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
            return _journal_summary(journal)
        journal["lastErrorCode"] = "operation_interrupted_no_mutation_replayed"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        return _journal_summary(journal)


def _add_common_plan_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the immutable active/candidate inputs and bounded policy settings."""

    parser.add_argument("--phase", choices=sorted(PHASES), required=True)
    parser.add_argument("--image-update-target", choices=sorted(IMAGE_UPDATE_TARGETS))
    parser.add_argument("--memory-update-target", choices=sorted(MEMORY_UPDATE_TARGETS))
    parser.add_argument(
        "--listener-repair",
        action="store_true",
        help="plan the bootstrap-only HF/Cloud wildcard-listener repair without changing images",
    )
    parser.add_argument("--active-compose-file", type=Path, action="append", required=True)
    parser.add_argument("--active-env-file", type=Path, action="append", required=True)
    parser.add_argument("--active-image-lock", type=Path, required=True)
    parser.add_argument("--candidate-compose-file", type=Path, action="append", required=True)
    parser.add_argument("--candidate-env-file", type=Path, action="append", required=True)
    parser.add_argument("--candidate-image-lock", type=Path, required=True)
    parser.add_argument("--secrets-dir", type=Path, required=True)
    parser.add_argument("--active-config-dir", type=Path, required=True)
    parser.add_argument("--candidate-config-dir", type=Path, required=True)
    parser.add_argument("--candidate-huggingface-seed", type=Path, required=True)
    parser.add_argument("--candidate-runpod-key-file", type=Path)
    parser.add_argument("--connector-auth-evidence", type=Path, required=True)
    parser.add_argument("--connector-auth-task-id", required=True)
    parser.add_argument("--account-center-unknown-preservation-evidence", type=Path)
    parser.add_argument("--notification-anchor-file", type=Path)
    parser.add_argument("--notification-provenance-file", type=Path)
    parser.add_argument("--active-selection", type=Path)
    parser.add_argument("--promote-selection", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--docker-prefix",
        choices=("docker", "sudo-n-docker"),
        default="sudo-n-docker",
    )
    parser.add_argument("--drain-timeout-seconds", type=int, default=300)
    parser.add_argument("--health-timeout-seconds", type=int, default=300)
    parser.add_argument("--max-memory-mib", type=int, default=3584)
    parser.add_argument("--max-cpus", type=float, default=1.95)
    parser.add_argument("--required-free-gib", type=float, default=2.0)
    parser.add_argument("--minimum-mem-available-mib", type=int, default=MEMORY_RESERVE_DEFAULT_MIB)
    parser.add_argument(
        "--ingress-exposure",
        choices=("public-cutover", "host-fronted-staging"),
        default="public-cutover",
    )
    parser.add_argument("--host-caddy-binary", default="/usr/local/bin/caddy")
    parser.add_argument("--openssl-binary", default="openssl")


def _build_parser() -> argparse.ArgumentParser:
    """Build the closed stage transition plan/apply/status/recovery interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan", help="create a private, read-only transition plan")
    _add_common_plan_arguments(plan_parser)
    apply_parser = commands.add_parser("apply", help="apply one confirmed transition plan")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--confirm-plan-sha256", required=True)
    status_parser = commands.add_parser("status", help="read one durable transition journal")
    status_parser.add_argument("--operation-id", required=True)
    status_parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    resume_parser = commands.add_parser(
        "resume", help="reconcile one interrupted operation read-only"
    )
    resume_parser.add_argument("--operation-id", required=True)
    resume_parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one formal plan/apply/status/recovery operation without unbounded Docker actions."""

    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.command == "plan":
            docker_prefix = (
                ["docker"] if arguments.docker_prefix == "docker" else ["sudo", "-n", "docker"]
            )
            runtime_update._validate_docker_prefix(docker_prefix)
            result = create_plan(
                phase=arguments.phase,
                image_update_target=arguments.image_update_target,
                memory_update_target=arguments.memory_update_target,
                listener_repair=arguments.listener_repair,
                active_compose_files=[path.resolve() for path in arguments.active_compose_file],
                active_env_files=[path.resolve() for path in arguments.active_env_file],
                active_image_lock=arguments.active_image_lock.resolve(),
                candidate_compose_files=[
                    path.resolve() for path in arguments.candidate_compose_file
                ],
                candidate_env_files=[path.resolve() for path in arguments.candidate_env_file],
                candidate_image_lock=arguments.candidate_image_lock.resolve(),
                secrets_dir=arguments.secrets_dir.resolve(),
                active_config_dir=arguments.active_config_dir.resolve(),
                candidate_config_dir=arguments.candidate_config_dir.resolve(),
                candidate_huggingface_seed=arguments.candidate_huggingface_seed.resolve(),
                candidate_runpod_key_file=(
                    arguments.candidate_runpod_key_file.resolve()
                    if arguments.candidate_runpod_key_file is not None
                    else None
                ),
                connector_auth_evidence=arguments.connector_auth_evidence.resolve(),
                connector_auth_task_id=arguments.connector_auth_task_id,
                account_center_unknown_preservation_evidence=(
                    arguments.account_center_unknown_preservation_evidence.resolve()
                    if arguments.account_center_unknown_preservation_evidence is not None
                    else None
                ),
                notification_anchor_file=(
                    arguments.notification_anchor_file.resolve()
                    if arguments.notification_anchor_file
                    else None
                ),
                notification_provenance_file=(
                    arguments.notification_provenance_file.resolve()
                    if arguments.notification_provenance_file
                    else None
                ),
                active_selection_file=(
                    arguments.active_selection.resolve()
                    if arguments.active_selection is not None
                    else None
                ),
                promote_selection_file=arguments.promote_selection.resolve(),
                backup_dir=arguments.backup_dir.resolve(),
                journal_dir=arguments.journal_dir.resolve(),
                plan_path=arguments.output.resolve(),
                docker_prefix=docker_prefix,
                drain_timeout_seconds=arguments.drain_timeout_seconds,
                health_timeout_seconds=arguments.health_timeout_seconds,
                max_memory_mib=arguments.max_memory_mib,
                max_cpus=arguments.max_cpus,
                required_free_gib=arguments.required_free_gib,
                minimum_mem_available_mib=arguments.minimum_mem_available_mib,
                ingress_exposure=arguments.ingress_exposure,
                host_caddy_binary=arguments.host_caddy_binary,
                openssl_binary=arguments.openssl_binary,
            )
        elif arguments.command == "apply":
            result = apply_plan_file(
                arguments.plan.resolve(),
                expected_digest=arguments.confirm_plan_sha256,
            )
        elif arguments.command == "status":
            result = operation_status(arguments.journal_dir.resolve(), arguments.operation_id)
        else:
            result = resume_operation(
                arguments.operation_id,
                journal_dir=arguments.journal_dir.resolve(),
            )
    except (
        OSError,
        StageTransitionError,
        runtime_update.RuntimeUpdateError,
        preflight.PreflightError,
        BuildError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Teams cloud stage transition blocked: {error}", file=sys.stderr)
        return 3 if arguments.command in {"apply", "resume"} else 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
