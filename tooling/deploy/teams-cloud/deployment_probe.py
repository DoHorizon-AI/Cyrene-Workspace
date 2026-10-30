"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 deployment_probe.py                                                  │
│  Module: tooling.deploy.teams_cloud.deployment_probe                    │
│  Role: Create fresh, scoped native connector probe receipts.             │
│                                                                          │
│  模块职责：从活动 selector 派生范围，通过 Navigator 原生 Exchange client │
│  进行只读连接器探测，并生成私有预检与通知 provenance 回执。            │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import preflight
import runtime_update
import stage_transition

POLICY_SCHEMA = "cyrene-teams-deployment-probe-policy-v1"
READINESS_SCHEMA = "cyrene-teams-deployment-probe-readiness-v1"
RESPONSES_SCHEMA = "cyrene-teams-deployment-probe-responses-v1"
ATTEMPT_SCHEMA = "cyrene-teams-deployment-probe-attempt-v1"
GITHUB_ACCEPTANCE_SCHEMA = "cyrene-github-readonly-acceptance-evidence-v1"
GITHUB_RESPONSES_SCHEMA = "cyrene-github-readonly-acceptance-responses-v1"
POLICY_OWNER_UID = 0
RUNTIME_OWNER_UID = 10001
DOCKER_PREFIX = ["docker"]
MAX_POLICY_BYTES = 256 * 1024
MAX_READINESS_BYTES = 16 * 1024 * 1024
MAX_NATIVE_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_HEALTH_WINDOW_SECONDS = 3600
MAX_GITHUB_ACCEPTANCE_SECONDS = 300
GITHUB_READ_OPERATION_ID = "github.repository.get"
GITHUB_REPOSITORY_PROJECTION_FIELDS = frozenset(
    {"repository_id", "repository", "private", "default_branch", "html_url"}
)
PROBE_OPERATIONS: dict[str, tuple[str, ...]] = {
    "huggingface": ("huggingface.repo.get",),
    "azure": ("cloud.resources.list", "cloud.provider.health.read"),
    "gcp": ("cloud.resources.list", "cloud.provider.health.read"),
    "runpod": ("cloud.resources.list", "cloud.provider.health.read"),
}
REQUIRED_PROVIDERS = frozenset({"huggingface", "azure", "gcp"})
EXPECTED_PROVIDERS = REQUIRED_PROVIDERS
OPTIONAL_PROVIDERS = frozenset({"runpod"})
HEALTH_TARGET_PROVIDERS = {
    "github-issues": frozenset({"github"}),
    "huggingface": frozenset({"huggingface"}),
    "cloud-management-phase2": frozenset({"azure", "gcp"}),
    "runpod": frozenset({"runpod"}),
}
NODE_PROBE = r"""
import { randomUUID } from 'node:crypto';
import { readdir } from 'node:fs/promises';
import path from 'node:path';

const moduleRoot = '/app/harness/dist';
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
let input;
try {
  input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
} catch {
  console.log(JSON.stringify({ status: 'FAIL', failureCode: 'INPUT_INVALID', results: [] }));
  process.exit(0);
}

let mod;
try {
  const files = await readdir(moduleRoot, { recursive: true });
  const matches = files.filter(name => name.endsWith('connector-authorization.js'));
  if (matches.length !== 1) throw new Error('module_lookup');
  mod = await import(path.join(moduleRoot, matches[0]));
} catch {
  console.log(JSON.stringify({ status: 'FAIL', failureCode: 'NATIVE_CLIENT_MODULE_UNAVAILABLE', results: [] }));
  process.exit(0);
}

const { ExchangeConnectorAuthorizationClient, ExchangeConnectorRequestError, canonicalJson, sha256 } = mod;
const env = process.env;
if (!env.CYRENE_EXCHANGE_CONNECTOR_INTERNAL_URL
  || !env.NAVIGATOR_EXCHANGE_HMAC_KEY_FILE
  || !env.NAVIGATOR_EXCHANGE_HMAC_KEY_ID) {
  console.log(JSON.stringify({ status: 'FAIL', failureCode: 'NATIVE_CLIENT_CONFIGURATION_UNAVAILABLE', results: [] }));
  process.exit(0);
}

const client = new ExchangeConnectorAuthorizationClient({
  baseUrl: env.CYRENE_EXCHANGE_CONNECTOR_INTERNAL_URL,
  hmacKeyFile: env.NAVIGATOR_EXCHANGE_HMAC_KEY_FILE,
  hmacKeyId: env.NAVIGATOR_EXCHANGE_HMAC_KEY_ID,
  audience: env.CYRENE_AUTHORITY_EXCHANGE_WORKLOAD_AUDIENCE ?? 'exchange',
});
const results = [];
for (const operation of input.operations) {
  const requestId = randomUUID();
  const resourceRef = 'sha256:' + sha256(canonicalJson({
    providerId: operation.providerId,
    profileRef: operation.profileRef,
    operationId: operation.operationId,
    target: operation.targetProjection,
  }));
  const argumentsDigestSha256 = sha256(canonicalJson(operation.arguments));
  const grantRequest = {
    requestId,
    workspaceId: input.actor.workspaceId,
    principalIssuer: input.actor.issuer,
    principalSubject: input.actor.subject,
    connectionRef: operation.connectionRef,
    providerId: operation.providerId,
    profileRef: operation.profileRef,
    operationId: operation.operationId,
    resourceRef,
    argumentsDigestSha256,
    arguments: operation.arguments,
  };
  try {
    const grant = await client.mintGrant(grantRequest, AbortSignal.timeout(10000));
    const response = await client.callProviderOperation({
      requestId,
      connectionRef: operation.connectionRef,
      providerId: operation.providerId,
      profileRef: operation.profileRef,
      operationId: operation.operationId,
      resourceRef,
      arguments: operation.arguments,
    }, grant, AbortSignal.timeout(10000));
    results.push({
      ...(input.includeConnectionRef === true ? { connectionRef: operation.connectionRef } : {}),
      providerId: operation.providerId,
      operationId: operation.operationId,
      outcome: response.outcome,
      observedAt: new Date().toISOString(),
      result: response.result,
    });
  } catch (error) {
    results.push({
      ...(input.includeConnectionRef === true ? { connectionRef: operation.connectionRef } : {}),
      providerId: operation.providerId,
      operationId: operation.operationId,
      outcome: 'failed',
      observedAt: new Date().toISOString(),
      failure: {
        errorType: typeof error?.name === 'string' ? error.name : 'Error',
        ...(error instanceof ExchangeConnectorRequestError ? {
          httpStatus: error.status ?? null,
          outcomeUnknown: error.outcomeUnknown,
        } : {}),
      },
    });
  }
}
const status = results.every(row => row.outcome === 'verified' && row.result?.isError !== true)
  ? 'PASS'
  : 'FAIL';
console.log(JSON.stringify({ status, results }));
"""


class DeploymentProbeError(ValueError):
    """A sanitized read-only probe, lineage, or scope validation failure."""


def _canonical_json(value: Any) -> bytes:
    """Serialize one JSON value deterministically for private fingerprints.

    中文：仅规范化序列化以计算私有摘要，不向日志输出正文。
    """

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    """Return the lowercase SHA-256 digest of one byte string."""

    return hashlib.sha256(raw).hexdigest()


def _utc_now() -> str:
    """Return a canonical UTC timestamp with a trailing Z."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc_timestamp(value: Any, label: str) -> datetime:
    """Parse one bounded UTC timestamp without imposing an evidence freshness window."""

    if not isinstance(value, str) or not value or len(value) > 64:
        raise DeploymentProbeError(f"{label} timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise DeploymentProbeError(f"{label} timestamp is malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise DeploymentProbeError(f"{label} timestamp must use UTC")
    return parsed.astimezone(UTC)


def _valid_text(value: Any, maximum: int) -> bool:
    """Return whether one bounded printable selector is safe for the closed DTO."""

    return isinstance(value, str) and 1 <= len(value) <= maximum and value.isprintable()


def _read_json(
    path: Path, label: str, *, maximum_bytes: int = MAX_POLICY_BYTES
) -> tuple[dict[str, Any], bytes]:
    """Read bounded JSON without following a symlink or accepting duplicate keys."""

    if path.is_symlink() or not path.is_file():
        raise DeploymentProbeError(f"{label} must be a regular non-symlink file")
    raw = path.read_bytes()
    if len(raw) > maximum_bytes:
        raise DeploymentProbeError(f"{label} exceeds its private size bound")
    try:
        document = json.loads(
            raw,
            object_pairs_hook=preflight._unique_json_object,
            parse_constant=runtime_update._reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise DeploymentProbeError(f"{label} is invalid JSON") from error
    if not isinstance(document, dict):
        raise DeploymentProbeError(f"{label} must be a JSON object")
    return document, raw


def _validate_policy_document(document: Any) -> dict[str, Any]:
    """Validate the closed per-deployment policy without interpreting arbitrary commands."""

    fields = {
        "schemaVersion",
        "owner",
        "runtime",
        "probeTargets",
        "notificationProvenanceRequired",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schemaVersion") != POLICY_SCHEMA
        or not isinstance(document.get("notificationProvenanceRequired"), bool)
    ):
        raise DeploymentProbeError("deployment probe policy has an unsupported closed schema")

    owner = document.get("owner")
    owner_fields = {"issuer", "subject", "organizationId", "workspaceId"}
    if (
        not isinstance(owner, dict)
        or set(owner) != owner_fields
        or any(
            not isinstance(owner.get(key), str)
            or not owner[key]
            or len(owner[key]) > 512
            or not owner[key].isprintable()
            for key in owner_fields
        )
    ):
        raise DeploymentProbeError("deployment probe owner is malformed")

    runtime = document.get("runtime")
    runtime_fields = {"selectionPath", "journalDirectory", "outputDirectory"}
    if not isinstance(runtime, dict) or set(runtime) != runtime_fields:
        raise DeploymentProbeError("deployment probe runtime paths are malformed")
    for key in runtime_fields:
        value = runtime.get(key)
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise DeploymentProbeError(f"deployment probe {key} must be an absolute path")

    targets = document.get("probeTargets")
    target_providers = frozenset(targets) if isinstance(targets, dict) else frozenset()
    if target_providers not in {REQUIRED_PROVIDERS, REQUIRED_PROVIDERS | OPTIONAL_PROVIDERS}:
        raise DeploymentProbeError(
            "deployment probe must select HF/Azure/GCP and may add only RunPod"
        )
    hf = targets.get("huggingface")
    if (
        not isinstance(hf, dict)
        or set(hf) != {"targetAlias", "repoType", "repoId"}
        or not _valid_text(hf.get("targetAlias"), 128)
        or hf.get("repoType") not in {"model", "dataset", "space"}
        or not _valid_text(hf.get("repoId"), 512)
        or "/" not in hf["repoId"]
    ):
        raise DeploymentProbeError("Hugging Face read target is malformed")
    azure = targets.get("azure")
    if (
        not isinstance(azure, dict)
        or set(azure) != {"targetAlias", "target"}
        or not _valid_text(azure.get("targetAlias"), 128)
        or not isinstance(azure.get("target"), dict)
        or set(azure["target"]) != {"resource_group"}
        or not _valid_text(azure["target"].get("resource_group"), 256)
    ):
        raise DeploymentProbeError("Azure read target must select one resource group")
    gcp = targets.get("gcp")
    if (
        not isinstance(gcp, dict)
        or set(gcp) != {"targetAlias", "target"}
        or not _valid_text(gcp.get("targetAlias"), 128)
        or not isinstance(gcp.get("target"), dict)
        or set(gcp["target"]) != {"zone"}
        or not _valid_text(gcp["target"].get("zone"), 256)
    ):
        raise DeploymentProbeError("GCP read target must select one zone")
    runpod = targets.get("runpod")
    if runpod is not None and (
        not isinstance(runpod, dict)
        or set(runpod) != {"targetAlias", "target"}
        or not _valid_text(runpod.get("targetAlias"), 128)
        or not isinstance(runpod.get("target"), dict)
        or set(runpod["target"]) != {"scope_ref"}
        or runpod["target"].get("scope_ref") != "pods"
    ):
        raise DeploymentProbeError("RunPod read target must use the fixed pods scope")
    return document


def _load_policy(path: Path) -> tuple[dict[str, Any], str]:
    """Require the controller-owned policy to be root-only and record its digest."""

    if os.geteuid() != POLICY_OWNER_UID:
        raise DeploymentProbeError("deployment probe policy must be read by its root controller")
    if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
        raise DeploymentProbeError("deployment probe policy path is unsafe")
    metadata = path.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != POLICY_OWNER_UID
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not preflight._private_artifact(path)
    ):
        raise DeploymentProbeError(
            "deployment probe policy must be a private root-owned mode-0600 artifact"
        )
    parent = path.parent.stat()
    if parent.st_uid != POLICY_OWNER_UID or stat.S_IMODE(parent.st_mode) != 0o700:
        raise DeploymentProbeError("deployment probe policy directory must be root-owned mode 0700")
    document, raw = _read_json(path, "deployment probe policy")
    return _validate_policy_document(document), _sha256_bytes(raw)


def _check_policy_unchanged(path: Path, expected_sha256: str) -> None:
    """Detect a policy replacement while a probe is in flight."""

    _document, observed_sha256 = _load_policy(path)
    if observed_sha256 != expected_sha256:
        raise DeploymentProbeError("deployment probe policy changed during the run")


def _validate_private_directory(path: Path, *, uid: int, label: str) -> Path:
    """Require an existing owner-only directory below the umbrella artifact root."""

    if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
        raise DeploymentProbeError(f"{label} directory path is unsafe")
    metadata = path.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or not preflight._private_artifact(path)
    ):
        raise DeploymentProbeError(f"{label} directory must be a private .artifacts directory")
    return path


def _write_private_json(path: Path, document: dict[str, Any]) -> str:
    """Create one non-overwriting UID-10001 mode-0600 receipt and return its digest."""

    _validate_private_directory(path.parent, uid=RUNTIME_OWNER_UID, label="probe output")
    if path.exists() or path.is_symlink():
        raise DeploymentProbeError("probe output already exists")
    raw = _canonical_json(document) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchown(descriptor, RUNTIME_OWNER_UID, RUNTIME_OWNER_UID)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return _sha256_bytes(raw)


def _verify_selector_record(
    selector: dict[str, Any],
    selector_path: Path,
    selector_bytes: bytes,
    journal_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Follow the active selector to its applied journal and exact accepted plan."""

    expected_selector_fields = {
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
        set(selector) != expected_selector_fields
        or selector.get("schemaVersion") != stage_transition.SELECTION_SCHEMA
        or selector.get("phase") != "issued-connector-activation"
        or not isinstance(selector.get("project"), str)
        or not isinstance(selector.get("planSha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", selector["planSha256"]) is None
    ):
        raise DeploymentProbeError("active runtime selector has an unsupported schema or phase")
    try:
        operation_id = str(uuid.UUID(selector.get("operationId")))
    except (ValueError, TypeError, AttributeError) as error:
        raise DeploymentProbeError("active selector operation ID is malformed") from error
    journal_path = stage_transition._journal_path(journal_dir, operation_id)
    journal = stage_transition._read_journal(journal_path)
    selection = journal.get("selection")
    if (
        journal.get("state") != "APPLIED"
        or journal.get("operationId") != operation_id
        or journal.get("planSha256") != selector["planSha256"]
        or not isinstance(selection, dict)
        or selection.get("promoted") is not True
        or selection.get("path") != str(selector_path)
        or selection.get("newSha256") != _sha256_bytes(selector_bytes)
    ):
        raise DeploymentProbeError("active selector is not backed by its exact applied journal")
    plan_path = Path(journal.get("planPath", ""))
    plan, _plan_bytes = _read_json(
        plan_path, "accepted deployment plan", maximum_bytes=16 * 1024 * 1024
    )
    stage_transition._validate_plan(plan)
    if (
        plan.get("operationId") != operation_id
        or plan.get("planSha256") != selector["planSha256"]
        or plan.get("phase") != selector["phase"]
        or plan.get("journalDir") != str(journal_dir)
        or plan.get("candidateSelectionFile") != str(selector_path)
        or plan.get("project") != selector["project"]
        or plan.get("candidateConfigDir") != selector["configDir"]
        or plan.get("activeSecretsDir") != selector["secretsDir"]
        or plan.get("candidateInputs", {}).get("composeFiles") != selector["composeFiles"]
        or plan.get("candidateInputs", {}).get("envFiles") != selector["envFiles"]
        or plan.get("candidateInputs", {}).get("imageLock") != selector["imageLock"]
        or plan.get("candidateInputs", {}).get("huggingfaceSeed") != selector["huggingfaceSeed"]
        or plan.get("candidateInputs", {}).get("runpodKey") != selector["runpodKey"]
        or plan.get("runtimeConfigIdentity", {}).get("candidate")
        != selector["runtimeConfigIdentity"]
    ):
        raise DeploymentProbeError("accepted plan does not match the active selector inputs")
    stage_transition._verify_all_inputs(plan)
    return journal, plan


def _load_active_runtime(policy: dict[str, Any]) -> dict[str, Any]:
    """Resolve the accepted selection, render its config, and pin live services."""

    runtime = policy["runtime"]
    selection_path = Path(runtime["selectionPath"])
    journal_dir = Path(runtime["journalDirectory"])
    for path, label in (
        (selection_path, "active selector"),
        (journal_dir, "deployment journal"),
    ):
        if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
            raise DeploymentProbeError(f"{label} path is unsafe")
    journal_metadata = journal_dir.stat()
    if (
        not stat.S_ISDIR(journal_metadata.st_mode)
        or journal_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(journal_metadata.st_mode) != 0o700
    ):
        raise DeploymentProbeError("deployment journal directory must be private and owner-only")
    selection_metadata = selection_path.stat()
    parent_metadata = selection_path.parent.stat()
    if (
        not stat.S_ISREG(selection_metadata.st_mode)
        or selection_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(selection_metadata.st_mode) != 0o600
        or parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise DeploymentProbeError("active selector must be a private owner-only runtime record")
    selector, selector_bytes = _read_json(selection_path, "active runtime selector")
    journal, plan = _verify_selector_record(selector, selection_path, selector_bytes, journal_dir)
    compose, lock = stage_transition._candidate_configs(plan, DOCKER_PREFIX)
    if compose.get("name") != selector.get("project"):
        raise DeploymentProbeError("rendered active Compose project differs from its selector")
    services = stage_transition._live_service_inventory(
        compose=compose,
        image_lock=lock,
        docker_prefix=DOCKER_PREFIX,
        mount_identity_by_service=plan["runtimeConfigIdentity"]["candidate"],
    )
    actor = preflight._connector_bootstrap_actor(compose)
    if actor != policy["owner"] or any(
        not isinstance(value, str) or not value for value in actor.values()
    ):
        raise DeploymentProbeError("active runtime owner differs from the root-owned probe policy")

    huggingface_seed_record = selector.get("huggingfaceSeed")
    if not isinstance(huggingface_seed_record, dict):
        raise DeploymentProbeError("active selector omitted its Hugging Face scope source")
    runpod_key_record = selector.get("runpodKey")
    scopes = preflight._connector_auth_scope_expectations(
        compose,
        huggingface_seed_file=Path(huggingface_seed_record["path"]),
        runpod_key_file=(
            Path(runpod_key_record["path"]) if isinstance(runpod_key_record, dict) else None
        ),
    )
    scope_digests = scopes.get("expectedProviderScopeDigests")
    if (
        not isinstance(scope_digests, dict)
        or frozenset(scope_digests)
        not in {REQUIRED_PROVIDERS, REQUIRED_PROVIDERS | OPTIONAL_PROVIDERS}
        or set(policy["probeTargets"]) != set(scope_digests)
    ):
        raise DeploymentProbeError(
            "active profile set differs from the selected HF/Azure/GCP/optional-RunPod probe contract"
        )
    if any(
        not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for digest in scope_digests.values()
    ):
        raise DeploymentProbeError("active provider scope digest is malformed")
    navigator_environment = compose.get("services", {}).get("navigator", {}).get("environment", {})
    notification_configured = (
        isinstance(navigator_environment, dict)
        and navigator_environment.get("TEAMS_CREDENTIAL_NOTIFICATIONS_CONFIG_FILE")
        == "/run/secrets/teams-credential-notifications.json"
    )
    if notification_configured != policy["notificationProvenanceRequired"]:
        raise DeploymentProbeError(
            "notification provenance policy differs from the accepted active configuration"
        )
    return {
        "selectionPath": selection_path,
        "selector": selector,
        "selectorBytes": selector_bytes,
        "selectorSha256": _sha256_bytes(selector_bytes),
        "journal": journal,
        "plan": plan,
        "compose": compose,
        "imageLock": lock,
        "services": services,
        "actor": actor,
        "scopeDigests": scope_digests,
        "scopeExpectations": scopes,
    }


def _verify_active_exchange_image(active: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Join live inventory's actual image ID to the rendered Compose lock entry."""

    exchange = active["services"].get("exchange")
    if not isinstance(exchange, dict):
        raise DeploymentProbeError("active Exchange service is missing")
    container_id = exchange.get("containerId")
    locked_ids = runtime_update._locked_image_ids(active["imageLock"])
    expected_image = stage_transition._service_image(active["compose"], locked_ids, "exchange")
    if (
        not isinstance(container_id, str)
        or stage_transition.FULL_CONTAINER_ID_RE.fullmatch(container_id) is None
        or exchange.get("imageRef") != expected_image["imageRef"]
        or exchange.get("imageId") != expected_image["imageId"]
    ):
        raise DeploymentProbeError("active Exchange container does not match its image lock")
    return container_id, expected_image


def _read_exchange_readiness(active: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Read one exact, credential-free Exchange readiness snapshot for this run."""

    container_id, _expected_image = _verify_active_exchange_image(active)
    actor = active["actor"]
    command = [
        *DOCKER_PREFIX,
        "exec",
        container_id,
        stage_transition.EXCHANGE_SECRET_ENV_ENTRYPOINT,
        stage_transition.EXCHANGE_READINESS_EXECUTABLE,
        "integrations",
        "connector-readiness",
        "--integration-database",
        stage_transition.EXCHANGE_INTEGRATION_DATABASE,
        "--issuer",
        actor["issuer"],
        "--subject",
        actor["subject"],
        "--organization-id",
        actor["organizationId"],
        "--workspace-id",
        actor["workspaceId"],
    ]
    try:
        output = runtime_update._checked_stdout(
            command,
            label="active Exchange connector readiness",
            timeout_seconds=120,
        )
    except (OSError, runtime_update.RuntimeUpdateError, subprocess.SubprocessError) as error:
        raise DeploymentProbeError("active Exchange readiness could not be read") from error
    encoded = output.encode("utf-8")
    if len(encoded) > MAX_READINESS_BYTES:
        raise DeploymentProbeError("Exchange readiness output exceeded its bound")
    try:
        snapshot = json.loads(
            output,
            object_pairs_hook=preflight._unique_json_object,
            parse_constant=runtime_update._reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise DeploymentProbeError("active Exchange readiness is invalid JSON") from error
    try:
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=snapshot,
            compose=active["compose"],
            phase=active["selector"]["phase"],
            include_live_catalog=True,
        )
    except (
        stage_transition.StageTransitionError,
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise DeploymentProbeError(
            "active Exchange readiness differs from the accepted bindings or catalog"
        ) from error
    if not isinstance(snapshot, dict) or snapshot.get("actor") != active["actor"]:
        raise DeploymentProbeError("active Exchange readiness actor differs from the selector")
    return snapshot, _sha256_bytes(encoded)


def _unique_provider_row(rows: Any, provider: str, label: str) -> dict[str, Any]:
    """Select exactly one provider row from the pinned Exchange readiness snapshot."""

    if not isinstance(rows, list):
        raise DeploymentProbeError(f"Exchange {label} inventory is malformed")
    matches = [row for row in rows if isinstance(row, dict) and row.get("providerId") == provider]
    if len(matches) != 1:
        raise DeploymentProbeError(f"Exchange readiness must contain one {provider} {label}")
    return matches[0]


def _cloud_profile_by_provider(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read active Azure/GCP profiles and the optional read-only RunPod profile."""

    service = compose.get("services", {}).get("cloud-management-phase2", {})
    document = preflight._json_environment_object(
        service, "CYRENE_CAPABILITY_CONFIGURATION_JSON", "Cloud Management"
    )
    rows = document.get("profiles")
    if not isinstance(rows, list):
        raise DeploymentProbeError("active cloud profiles are malformed")
    profiles: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("provider") not in {
            "azure",
            "gcp",
            "runpod",
        }:
            raise DeploymentProbeError("active cloud provider set contains an unsupported profile")
        provider = row["provider"]
        if provider in profiles:
            raise DeploymentProbeError(f"active cloud configuration duplicates {provider}")
        profiles[provider] = row
    if set(profiles) not in ({"azure", "gcp"}, {"azure", "gcp", "runpod"}):
        raise DeploymentProbeError(
            "active cloud configuration must include Azure/GCP and optional RunPod"
        )
    if "runpod" in profiles and profiles["runpod"].get("read_only") is not True:
        raise DeploymentProbeError("active RunPod profile must enforce read_only=true")
    return profiles


def _build_probe_operations(
    *,
    snapshot: dict[str, Any],
    compose: dict[str, Any],
    targets: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Build the fixed read calls after joining policy targets to active scopes."""

    cloud_profiles = _cloud_profile_by_provider(compose)
    operations: list[dict[str, Any]] = []
    auth_modes: dict[str, str] = {}
    for provider in ("huggingface", "azure", "gcp", "runpod"):
        if provider not in targets:
            continue
        connection = _unique_provider_row(snapshot.get("connections"), provider, "connection")
        profile = _unique_provider_row(snapshot.get("profiles"), provider, "profile")
        binding = _unique_provider_row(snapshot.get("bindings"), provider, "binding")
        target_policy = targets[provider]
        connection_ref = connection.get("connectionRef")
        profile_ref = connection.get("profileRef")
        binding_id = binding.get("bindingId")
        snapshot_auth_mode = profile.get("authMode")
        if (
            not isinstance(connection_ref, str)
            or not connection_ref
            or not isinstance(profile_ref, str)
            or not profile_ref
            or not isinstance(binding_id, str)
            or not binding_id
            or profile.get("profileRef") != profile_ref
            or binding.get("connectionRef") != connection_ref
            or binding.get("profileRef") != profile_ref
            or binding.get("providerId") != provider
        ):
            raise DeploymentProbeError(f"active {provider} readiness identity does not join")
        tools = binding.get("tools")
        if not isinstance(tools, list):
            raise DeploymentProbeError(f"active {provider} binding tool list is malformed")
        if provider == "runpod":
            runpod_operation_ids = [
                row.get("operationId") for row in tools if isinstance(row, dict)
            ]
            runpod_read_operations = preflight.CONNECTOR_MODEL_READONLY_OPERATIONS["runpod"]
            if (
                len(runpod_operation_ids) != len(tools)
                or len(set(runpod_operation_ids)) != len(runpod_operation_ids)
                or not set(PROBE_OPERATIONS[provider]) <= set(runpod_operation_ids)
                or not set(runpod_operation_ids) <= runpod_read_operations
                or any(
                    not isinstance(row, dict) or row.get("mutating") is not False for row in tools
                )
            ):
                raise DeploymentProbeError(
                    "RunPod binding must contain the fixed probe reads and only supported read operations"
                )
        catalog = snapshot.get("catalog")
        catalog_rows = catalog.get("items") if isinstance(catalog, dict) else None
        if not isinstance(catalog_rows, list):
            raise DeploymentProbeError("active Exchange catalog is malformed")

        if provider == "huggingface":
            actual_scope = connection.get("targetScope")
            if (
                connection.get("targetAlias") != target_policy["targetAlias"]
                or not isinstance(actual_scope, dict)
                or set(actual_scope) != {"permissions", "repoId", "repoType"}
                or actual_scope.get("repoType") != target_policy["repoType"]
                or actual_scope.get("repoId") != target_policy["repoId"]
                or actual_scope.get("permissions") != ["read"]
            ):
                raise DeploymentProbeError(
                    "Hugging Face probe target differs from its active read-only repository scope"
                )
            call_specs = [
                (
                    "huggingface.repo.get",
                    {
                        "repo_type": target_policy["repoType"],
                        "repo_id": target_policy["repoId"],
                    },
                    {"repo_type": target_policy["repoType"], "repo_id": target_policy["repoId"]},
                )
            ]
        else:
            if connection.get("targetAlias") != target_policy["targetAlias"]:
                raise DeploymentProbeError(
                    f"{provider} probe target alias differs from active Exchange"
                )
            active_profile = cloud_profiles[provider]
            if active_profile.get("profile_ref") != profile_ref:
                raise DeploymentProbeError(
                    f"active {provider} scope differs from its Exchange profile"
                )
            active_auth = active_profile.get("auth")
            auth_mode = active_auth.get("mode") if isinstance(active_auth, dict) else None
            if not isinstance(auth_mode, str) or not auth_mode:
                raise DeploymentProbeError(f"active {provider} authentication mode is missing")
            if snapshot_auth_mode is not None and snapshot_auth_mode != auth_mode:
                raise DeploymentProbeError(
                    f"active {provider} readiness authentication mode differs from its rendered profile"
                )
            if auth_mode not in preflight.CONNECTOR_AUTH_READONLY_AUTH_MODES.get(provider, set()):
                raise DeploymentProbeError(
                    f"active {provider} rendered profile has an unsupported authentication mode"
                )
            target = target_policy["target"]
            scope = active_profile.get("scope")
            if provider == "runpod":
                if (
                    target != {"scope_ref": "pods"}
                    or active_profile.get("read_only") is not True
                    or not _valid_text(active_profile.get("identity_ref"), 256)
                    or not _valid_text(active_profile.get("profile_ref"), 256)
                ):
                    raise DeploymentProbeError(
                        "RunPod probe must use its fixed read-only pods scope"
                    )
            elif not isinstance(scope, dict):
                raise DeploymentProbeError(f"active {provider} scope is malformed")
            elif provider == "azure":
                groups = scope.get("resource_groups")
                if (
                    not isinstance(scope.get("subscription_id"), str)
                    or not isinstance(groups, list)
                    or target["resource_group"] not in groups
                ):
                    raise DeploymentProbeError(
                        "Azure resource group is outside the active subscription scope"
                    )
            else:
                zones = scope.get("zones")
                prefixes = scope.get("instance_name_prefixes")
                if (
                    not isinstance(scope.get("project_id"), str)
                    or not isinstance(zones, list)
                    or target["zone"] not in zones
                    or not isinstance(prefixes, list)
                    or not prefixes
                ):
                    raise DeploymentProbeError("GCP zone is outside the active project scope")
            exchange_scope = connection.get("targetScope")
            profile_scope = profile.get("targetScope")
            targets_map = profile_scope.get("targets") if isinstance(profile_scope, dict) else None
            target_ref = (
                targets_map.get(target_policy["targetAlias"])
                if isinstance(targets_map, dict)
                else None
            )
            if (
                not isinstance(exchange_scope, dict)
                or exchange_scope.get("targetAlias") != target_policy["targetAlias"]
                or not isinstance(target_ref, dict)
                or exchange_scope.get("targetRef") != target_ref.get("targetRef")
            ):
                raise DeploymentProbeError(
                    f"{provider} target alias is not in its active Exchange scope"
                )
            call_specs = [
                (
                    "cloud.resources.list",
                    {"profile_ref": profile_ref, "target": target},
                    target,
                ),
                (
                    "cloud.provider.health.read",
                    {"profile_ref": profile_ref},
                    {"profile_ref": profile_ref},
                ),
            ]

        if provider == "huggingface":
            auth_mode = snapshot_auth_mode
            if auth_mode not in preflight.CONNECTOR_AUTH_READONLY_AUTH_MODES.get(provider, set()):
                raise DeploymentProbeError(
                    "Hugging Face readiness authentication mode is unsupported"
                )
        auth_modes[provider] = auth_mode
        for operation_id, arguments, target_projection in call_specs:
            operation_policy = preflight.CONNECTOR_OPERATION_POLICIES.get(operation_id)
            if (
                not isinstance(operation_policy, tuple)
                or len(operation_policy) != 3
                or operation_policy[2] is not False
            ):
                raise DeploymentProbeError(f"{operation_id} is not a canonical read-only operation")
            matches = [
                row
                for row in tools
                if isinstance(row, dict) and row.get("operationId") == operation_id
            ]
            if len(matches) != 1 or matches[0].get("mutating") is not False:
                raise DeploymentProbeError(
                    f"active {provider} binding does not authorize one read-only {operation_id}"
                )
            matching_catalog = [
                row
                for row in catalog_rows
                if isinstance(row, dict) and row.get("bindingId") == binding_id
            ]
            if len(matching_catalog) != 1:
                raise DeploymentProbeError(
                    f"active {provider} catalog binding is missing or duplicate"
                )
            catalog_tools = matching_catalog[0].get("tools")
            if provider == "runpod":
                expected_tool_ids = {
                    preflight.CONNECTOR_OPERATION_POLICIES[operation][0]
                    for operation in runpod_operation_ids
                }
                if (
                    not isinstance(catalog_tools, list)
                    or len(catalog_tools) != len(expected_tool_ids)
                    or {
                        row.get("id")
                        for row in catalog_tools
                        if isinstance(row, dict) and row.get("readOnly") is True
                    }
                    != expected_tool_ids
                    or any(
                        not isinstance(row, dict) or row.get("readOnly") is not True
                        for row in catalog_tools
                    )
                ):
                    raise DeploymentProbeError(
                        "RunPod catalog must expose only its authorized read-only operations"
                    )
            tool_id = operation_policy[0]
            tool_rows = (
                [row for row in catalog_tools if isinstance(row, dict) and row.get("id") == tool_id]
                if isinstance(catalog_tools, list)
                else []
            )
            if len(tool_rows) != 1 or tool_rows[0].get("readOnly") is not True:
                raise DeploymentProbeError(
                    f"active catalog does not expose {operation_id} as read-only"
                )
            operations.append(
                {
                    "providerId": provider,
                    "connectionRef": connection_ref,
                    "profileRef": profile_ref,
                    **(
                        {"providerIdentityRef": active_profile["identity_ref"]}
                        if provider == "runpod"
                        else {}
                    ),
                    "operationId": operation_id,
                    "arguments": arguments,
                    "targetProjection": target_projection,
                }
            )
    expected = {
        (provider, operation) for provider in targets for operation in PROBE_OPERATIONS[provider]
    }
    if (
        len(operations) != len(expected)
        or {(row["providerId"], row["operationId"]) for row in operations} != expected
    ):
        raise DeploymentProbeError("native probe plan differs from its fixed operation allowlist")
    return operations, auth_modes


def _github_repository_scope(value: Any, label: str) -> dict[str, str]:
    """Normalize one exact GitHub repository scope without widening its target."""

    if not isinstance(value, dict):
        raise DeploymentProbeError(f"{label} GitHub repository scope is malformed")
    if set(value) == {"repositoryId", "repository"}:
        repository_id = value.get("repositoryId")
        repository = value.get("repository")
    elif set(value) == {"repositoryId", "owner", "name"}:
        repository_id = value.get("repositoryId")
        owner = value.get("owner")
        name = value.get("name")
        if not _valid_text(owner, 256) or not _valid_text(name, 256):
            raise DeploymentProbeError(f"{label} GitHub repository scope is malformed")
        repository = f"{owner}/{name}"
    else:
        raise DeploymentProbeError(f"{label} GitHub repository scope has unexpected fields")
    if (
        not isinstance(repository_id, str)
        or re.fullmatch(r"[0-9]{1,100}", repository_id) is None
        or not _valid_text(repository, 512)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
    ):
        raise DeploymentProbeError(f"{label} GitHub repository scope is malformed")
    return {"repository_id": repository_id, "repository": repository}


def _github_repository_input_schema_is_closed(value: Any) -> bool:
    """Require the official get-repository input schema to accept only its two scope fields."""

    if not isinstance(value, dict) or set(value) != {
        "type",
        "properties",
        "required",
        "additionalProperties",
    }:
        return False
    properties = value.get("properties")
    return (
        value.get("type") == "object"
        and isinstance(properties, dict)
        and set(properties) == {"repository_id", "repository"}
        and all(
            isinstance(properties.get(name), dict) and properties[name].get("type") == "string"
            for name in ("repository_id", "repository")
        )
        and value.get("required") == ["repository_id", "repository"]
        and value.get("additionalProperties") is False
    )


def _build_github_read_operations(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Join all ten GitHub connections to the frozen read-only repository-get catalog."""

    connections = snapshot.get("connections")
    bindings = snapshot.get("bindings")
    profiles = snapshot.get("profiles")
    catalog = snapshot.get("catalog")
    catalog_items = catalog.get("items") if isinstance(catalog, dict) else None
    if not all(isinstance(rows, list) for rows in (connections, bindings, profiles, catalog_items)):
        raise DeploymentProbeError("active GitHub readiness inventories are malformed")
    github_connections = [
        row for row in connections if isinstance(row, dict) and row.get("providerId") == "github"
    ]
    github_bindings = [
        row for row in bindings if isinstance(row, dict) and row.get("providerId") == "github"
    ]
    if len(github_connections) != 10 or len(github_bindings) != 10:
        raise DeploymentProbeError(
            "active GitHub acceptance requires exactly ten bound repositories"
        )

    github_profiles = [
        row for row in profiles if isinstance(row, dict) and row.get("providerId") == "github"
    ]
    profile_by_ref: dict[str, dict[str, Any]] = {}
    profile_targets: dict[str, set[tuple[str, str]]] = {}
    for profile in github_profiles:
        profile_ref = profile.get("profileRef")
        scope = profile.get("targetScope")
        repositories = scope.get("repositories") if isinstance(scope, dict) else None
        if (
            not isinstance(profile_ref, str)
            or not profile_ref
            or profile_ref in profile_by_ref
            or profile.get("authMode") != "github_app"
            or profile.get("authStatus") != "healthy"
            or profile.get("accessMode") != "workspace_operator"
            or not isinstance(scope, dict)
            or set(scope) != {"repositories"}
            or not isinstance(repositories, list)
            or not repositories
        ):
            raise DeploymentProbeError("active GitHub profile metadata is not healthy and closed")
        normalized_targets: set[tuple[str, str]] = set()
        for repository_row in repositories:
            repository = _github_repository_scope(repository_row, "profile")
            key = (repository["repository_id"], repository["repository"])
            if key in normalized_targets:
                raise DeploymentProbeError("active GitHub profile has duplicate repository scope")
            normalized_targets.add(key)
        profile_by_ref[profile_ref] = profile
        profile_targets[profile_ref] = normalized_targets
    if not profile_by_ref:
        raise DeploymentProbeError("active GitHub profile metadata is missing")

    catalog_by_binding: dict[str, dict[str, Any]] = {}
    for item in catalog_items:
        if not isinstance(item, dict) or not isinstance(item.get("bindingId"), str):
            raise DeploymentProbeError("active GitHub catalog binding row is malformed")
        if item["bindingId"] in catalog_by_binding:
            raise DeploymentProbeError("active GitHub catalog contains a duplicate binding")
        catalog_by_binding[item["bindingId"]] = item

    connection_by_ref: dict[str, dict[str, Any]] = {}
    for connection in github_connections:
        reference = connection.get("connectionRef")
        try:
            canonical_ref = str(uuid.UUID(reference))
        except (ValueError, TypeError, AttributeError) as error:
            raise DeploymentProbeError("active GitHub connection reference is malformed") from error
        if canonical_ref != reference or canonical_ref in connection_by_ref:
            raise DeploymentProbeError(
                "active GitHub connection reference is noncanonical or duplicated"
            )
        target = _github_repository_scope(connection.get("targetScope"), "connection")
        profile_ref = connection.get("profileRef")
        if not isinstance(profile_ref, str) or profile_ref not in profile_by_ref:
            raise DeploymentProbeError("active GitHub connection has no joined profile")
        if (target["repository_id"], target["repository"]) not in profile_targets[profile_ref]:
            raise DeploymentProbeError(
                "active GitHub connection target is outside its profile scope"
            )
        connection_by_ref[canonical_ref] = {
            "connectionRef": canonical_ref,
            "profileRef": profile_ref,
            "arguments": target,
        }

    operations: list[dict[str, Any]] = []
    seen_binding_refs: set[str] = set()
    profile_targets_seen: dict[str, set[tuple[str, str]]] = {
        profile_ref: set() for profile_ref in profile_by_ref
    }
    for binding in github_bindings:
        binding_id = binding.get("bindingId")
        connection_ref = binding.get("connectionRef")
        connection = connection_by_ref.get(connection_ref)
        if (
            not isinstance(binding_id, str)
            or not isinstance(connection_ref, str)
            or connection is None
            or connection_ref in seen_binding_refs
            or binding.get("profileRef") != connection["profileRef"]
        ):
            raise DeploymentProbeError("active GitHub binding does not join its unique connection")
        seen_binding_refs.add(connection_ref)
        tools = binding.get("tools")
        tool_rows = tools if isinstance(tools, list) else []
        matches = [
            tool
            for tool in tool_rows
            if isinstance(tools, list)
            and isinstance(tool, dict)
            and tool.get("operationId") == GITHUB_READ_OPERATION_ID
        ]
        if (
            len(matches) != 1
            or matches[0].get("toolId") != GITHUB_READ_OPERATION_ID
            or matches[0].get("resourceProjection") != "github_repository"
            or matches[0].get("mutating") is not False
        ):
            raise DeploymentProbeError(
                "GitHub binding does not authorize the fixed read-only repository operation"
            )
        catalog_row = catalog_by_binding.get(binding_id)
        catalog_tools = catalog_row.get("tools") if isinstance(catalog_row, dict) else None
        catalog_tool_rows = catalog_tools if isinstance(catalog_tools, list) else []
        descriptors = [
            tool
            for tool in catalog_tool_rows
            if isinstance(catalog_tools, list)
            and isinstance(tool, dict)
            and tool.get("id") == GITHUB_READ_OPERATION_ID
        ]
        if (
            len(descriptors) != 1
            or descriptors[0].get("readOnly") is not True
            or not _github_repository_input_schema_is_closed(descriptors[0].get("inputSchema"))
        ):
            raise DeploymentProbeError(
                "GitHub catalog does not expose the exact read-only repository schema"
            )
        profile_ref = connection["profileRef"]
        args = connection["arguments"]
        profile_targets_seen[profile_ref].add((args["repository_id"], args["repository"]))
        operations.append(
            {
                "providerId": "github",
                "connectionRef": connection_ref,
                "profileRef": profile_ref,
                "operationId": GITHUB_READ_OPERATION_ID,
                "arguments": args,
                "targetProjection": args,
            }
        )

    if (
        seen_binding_refs != set(connection_by_ref)
        or len(
            {
                (row["arguments"]["repository_id"], row["arguments"]["repository"])
                for row in operations
            }
        )
        != 10
        or any(profile_targets_seen[ref] != profile_targets[ref] for ref in profile_by_ref)
    ):
        raise DeploymentProbeError(
            "GitHub bindings do not cover the exact ten profile repository scopes"
        )
    operations.sort(key=lambda row: row["connectionRef"])
    return operations


def _extract_github_repository_projection(value: Any) -> dict[str, Any]:
    """Read the official plugin's repository projection from its JSON tool result."""

    candidates: list[dict[str, Any]] = []
    if isinstance(value, dict) and set(value) == GITHUB_REPOSITORY_PROJECTION_FIELDS:
        candidates.append(value)
    elif isinstance(value, dict):
        if value.get("isError") is True or set(value) - {"content", "isError", "structuredContent"}:
            raise DeploymentProbeError("GitHub provider returned an unsupported result envelope")
        structured = value.get("structuredContent")
        if structured is not None:
            if (
                not isinstance(structured, dict)
                or set(structured) != GITHUB_REPOSITORY_PROJECTION_FIELDS
            ):
                raise DeploymentProbeError(
                    "GitHub provider structured repository result is malformed"
                )
            candidates.append(structured)
        content = value.get("content")
        if content is not None:
            if not isinstance(content, list) or len(content) != 1:
                raise DeploymentProbeError("GitHub provider must return one JSON content block")
            block = content[0]
            if (
                not isinstance(block, dict)
                or set(block) != {"type", "text"}
                or block.get("type") != "text"
            ):
                raise DeploymentProbeError("GitHub provider returned an unsupported content block")
            try:
                parsed = json.loads(
                    block["text"],
                    object_pairs_hook=preflight._unique_json_object,
                    parse_constant=runtime_update._reject_json_constant,
                )
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise DeploymentProbeError("GitHub provider JSON content is malformed") from error
            if not isinstance(parsed, dict) or set(parsed) != GITHUB_REPOSITORY_PROJECTION_FIELDS:
                raise DeploymentProbeError("GitHub provider JSON projection has unexpected fields")
            candidates.append(parsed)
        if not candidates:
            raise DeploymentProbeError("GitHub provider returned no repository projection")
    else:
        raise DeploymentProbeError("GitHub provider repository result is malformed")

    projection = candidates[0]
    if any(candidate != projection for candidate in candidates[1:]):
        raise DeploymentProbeError("GitHub provider result projections disagree")
    if (
        not isinstance(projection.get("repository_id"), str)
        or not re.fullmatch(r"[0-9]{1,100}", projection["repository_id"])
        or not _valid_text(projection.get("repository"), 512)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", projection["repository"]) is None
        or not isinstance(projection.get("private"), bool)
        or (
            projection.get("default_branch") is not None
            and not isinstance(projection.get("default_branch"), str)
        )
        or (
            projection.get("html_url") is not None
            and not isinstance(projection.get("html_url"), str)
        )
    ):
        raise DeploymentProbeError("GitHub provider repository projection is malformed")
    return projection


def _extract_runpod_health_projection(value: Any) -> dict[str, Any]:
    """Read one RunPod health payload from a consistent typed MCP result."""

    expected_fields = {
        "profile_ref",
        "provider",
        "identity_ref",
        "authentication_state",
        "authorization_state",
        "scope_refs",
        "checked_at",
        "expires_at",
        "reason_code",
    }
    candidates: list[dict[str, Any]] = []
    if isinstance(value, dict) and set(value) == expected_fields:
        candidates.append(value)
    elif isinstance(value, dict):
        if value.get("isError") is True or set(value) - {
            "content",
            "isError",
            "structuredContent",
        }:
            raise DeploymentProbeError("RunPod health result envelope is unsupported")
        structured = value.get("structuredContent")
        if structured is not None:
            if not isinstance(structured, dict) or set(structured) != expected_fields:
                raise DeploymentProbeError("RunPod structured health result is malformed")
            candidates.append(structured)
        content = value.get("content")
        if content is not None:
            if not isinstance(content, list) or len(content) != 1:
                raise DeploymentProbeError(
                    "RunPod health result must contain one JSON content block"
                )
            block = content[0]
            if (
                not isinstance(block, dict)
                or set(block) != {"type", "text"}
                or block["type"] != "text"
            ):
                raise DeploymentProbeError("RunPod health result content block is unsupported")
            try:
                parsed = json.loads(
                    block["text"],
                    object_pairs_hook=preflight._unique_json_object,
                    parse_constant=runtime_update._reject_json_constant,
                )
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise DeploymentProbeError("RunPod health JSON content is malformed") from error
            if not isinstance(parsed, dict) or set(parsed) != expected_fields:
                raise DeploymentProbeError("RunPod health JSON projection is malformed")
            candidates.append(parsed)
        if not candidates:
            raise DeploymentProbeError("RunPod health result contains no typed payload")
    else:
        raise DeploymentProbeError("RunPod health result is malformed")
    if any(candidate != candidates[0] for candidate in candidates[1:]):
        raise DeploymentProbeError("RunPod health projections disagree")
    return candidates[0]


def _runpod_health_succeeded(result: Any, operation: dict[str, Any]) -> bool:
    """Require a fresh, owner-bound RunPod health receipt with both states valid."""

    try:
        projection = _extract_runpod_health_projection(result)
        if (
            projection["provider"] != "runpod"
            or projection["profile_ref"] != operation.get("profileRef")
            or projection["identity_ref"] != operation.get("providerIdentityRef")
            or projection["authentication_state"] != "VALID"
            or projection["authorization_state"] != "VALID"
            or projection["scope_refs"] != [f"runpod:{operation.get('profileRef')}"]
            or projection["reason_code"] is not None
            or not isinstance(projection["checked_at"], str)
        ):
            return False
        preflight._read_timestamp(projection["checked_at"], "RunPod health checked_at")
        expires_at = projection["expires_at"]
        if expires_at is not None:
            if not isinstance(expires_at, str):
                return False
            if _parse_utc_timestamp(expires_at, "RunPod health expires_at") < datetime.now(UTC):
                return False
    except (DeploymentProbeError, preflight.PreflightError, KeyError, TypeError, ValueError):
        return False
    return True


def _validate_github_native_results(
    envelope: dict[str, Any], operations: list[dict[str, Any]]
) -> tuple[bool, list[dict[str, Any]], str]:
    """Bind every GitHub response to its connection and verify returned repository identity."""

    expected = {row["connectionRef"]: row for row in operations}
    rows = envelope.get("results")
    if not isinstance(rows, list) or len(rows) != len(expected) or len(expected) != 10:
        raise DeploymentProbeError("GitHub result count differs from the exact ten-connection plan")
    observed_at_by_ref: dict[str, dict[str, Any]] = {}
    verified_at = _utc_now()
    verified_datetime = datetime.fromisoformat(verified_at)
    outcomes: list[dict[str, Any]] = []
    success = envelope.get("status") == "PASS"
    for row in rows:
        if not isinstance(row, dict):
            raise DeploymentProbeError("GitHub result row is malformed")
        connection_ref = row.get("connectionRef")
        operation = expected.get(connection_ref)
        if (
            not isinstance(connection_ref, str)
            or operation is None
            or connection_ref in observed_at_by_ref
            or row.get("providerId") != "github"
            or row.get("operationId") != GITHUB_READ_OPERATION_ID
        ):
            raise DeploymentProbeError(
                "GitHub responses are not uniquely joined to all ten connections"
            )
        observed_at = row.get("observedAt")
        observed_time = preflight._read_timestamp(observed_at, "GitHub repository observation")
        if observed_time > verified_datetime:
            raise DeploymentProbeError("GitHub repository observation occurred after verification")
        observed_at_by_ref[connection_ref] = row
        outcome = row.get("outcome")
        result = row.get("result")
        result_digest = _sha256_bytes(_canonical_json(result)) if isinstance(result, dict) else None
        failure_code: str | None = None
        if outcome != "verified" or not isinstance(result, dict):
            failure_code = "PROVIDER_OPERATION_NOT_VERIFIED"
        else:
            try:
                projection = _extract_github_repository_projection(result)
            except DeploymentProbeError:
                failure_code = "REPOSITORY_RESPONSE_INVALID"
            else:
                expected_arguments = operation["arguments"]
                if (
                    projection["repository_id"] != expected_arguments["repository_id"]
                    or projection["repository"] != expected_arguments["repository"]
                ):
                    failure_code = "REPOSITORY_SCOPE_MISMATCH"
        succeeded = failure_code is None
        success = success and succeeded
        outcomes.append(
            {
                "connectionRefSha256": _sha256_bytes(connection_ref.encode("utf-8")),
                "profileRefSha256": _sha256_bytes(operation["profileRef"].encode("utf-8")),
                "targetSha256": _sha256_bytes(_canonical_json(operation["targetProjection"])),
                "operationId": GITHUB_READ_OPERATION_ID,
                "observedAt": observed_at,
                "outcome": "SUCCEEDED" if succeeded else "FAILED",
                "failureCode": failure_code,
                "resultSha256": result_digest,
            }
        )
    if set(observed_at_by_ref) != set(expected):
        raise DeploymentProbeError("GitHub responses omitted one or more connection references")
    outcomes.sort(key=lambda row: row["connectionRefSha256"])
    return success, outcomes, verified_at


def _write_attempt_marker(
    output_dir: Path,
    run_id: str,
    policy_sha256: str,
    *,
    name_prefix: str = "deployment-probe",
    provider_ids: list[str] | None = None,
    operation_count: int | None = None,
) -> Path:
    """Reserve one unique read-only attempt before any provider call."""

    providers = sorted(provider_ids if provider_ids is not None else EXPECTED_PROVIDERS)
    count = (
        operation_count
        if operation_count is not None
        else sum(len(PROBE_OPERATIONS[provider]) for provider in providers)
    )
    marker = output_dir / f"{name_prefix}-{run_id}.started.private.json"
    _write_private_json(
        marker,
        {
            "schemaVersion": ATTEMPT_SCHEMA,
            "runId": run_id,
            "state": "RESERVED_BEFORE_PROVIDER_CALLS",
            "reservedAt": _utc_now(),
            "policySha256": policy_sha256,
            "providerIds": providers,
            "operationCount": count,
        },
    )
    return marker


def _execute_native_probe(
    navigator_container_id: str,
    operations: list[dict[str, Any]],
    actor: dict[str, str],
    *,
    include_connection_ref: bool = False,
    timeout_seconds: int = 180,
) -> tuple[dict[str, Any], bytes]:
    """Run the one sequential native HMAC/Exchange probe inside the pinned Navigator."""

    if not 30 <= timeout_seconds <= MAX_GITHUB_ACCEPTANCE_SECONDS:
        raise DeploymentProbeError("native Exchange call deadline is outside its fixed bound")
    payload = _canonical_json(
        {
            "actor": actor,
            "operations": operations,
            "includeConnectionRef": include_connection_ref,
        }
    )
    command = [
        *DOCKER_PREFIX,
        "exec",
        "-i",
        navigator_container_id,
        "/usr/local/bin/cyrene-secret-env-entrypoint",
        "node",
        "--input-type=module",
        "-e",
        NODE_PROBE,
    ]
    try:
        result = subprocess.run(
            command,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        partial = error.stdout or b""
        if isinstance(partial, str):
            partial = partial.encode("utf-8", errors="replace")
        if len(partial) > MAX_NATIVE_OUTPUT_BYTES:
            partial = partial[:MAX_NATIVE_OUTPUT_BYTES]
        return {
            "status": "UNKNOWN",
            "failureCode": "NATIVE_CLIENT_TOTAL_TIMEOUT",
            "results": [],
        }, partial
    except OSError as error:
        raise DeploymentProbeError("native Exchange client could not be started") from error
    raw = result.stdout
    if len(raw) > MAX_NATIVE_OUTPUT_BYTES:
        raise DeploymentProbeError("native Exchange responses exceeded their private output bound")
    if result.returncode != 0:
        return {
            "status": "UNKNOWN",
            "failureCode": "NATIVE_CLIENT_EXIT_NONZERO",
            "results": [],
        }, raw
    try:
        envelope = json.loads(
            raw,
            object_pairs_hook=preflight._unique_json_object,
            parse_constant=runtime_update._reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise DeploymentProbeError(
            "native Exchange client returned invalid bounded JSON"
        ) from error
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"status", "results"}
        or envelope.get("status") not in {"PASS", "FAIL"}
        or not isinstance(envelope.get("results"), list)
    ):
        raise DeploymentProbeError("native Exchange client returned an unsupported result envelope")
    return envelope, raw


def _validate_native_results(
    envelope: dict[str, Any],
    expected_operations: list[dict[str, Any]],
) -> tuple[bool, list[dict[str, Any]], str]:
    """Require exactly one bounded response for each selected read operation."""

    expected_by_pair = {(row["providerId"], row["operationId"]): row for row in expected_operations}
    expected_pairs = set(expected_by_pair)
    rows = envelope.get("results")
    if not isinstance(rows, list) or len(rows) != len(expected_pairs):
        raise DeploymentProbeError("native response count differs from the fixed operation plan")
    observed_pairs: set[tuple[str, str]] = set()
    verified_at = _utc_now()
    verified_datetime = datetime.fromisoformat(verified_at)
    summary_rows: list[dict[str, Any]] = []
    all_success = envelope.get("status") == "PASS"
    for row in rows:
        if not isinstance(row, dict):
            raise DeploymentProbeError("native operation result row is malformed")
        provider = row.get("providerId")
        operation_id = row.get("operationId")
        pair = (provider, operation_id)
        if pair not in expected_pairs or pair in observed_pairs:
            raise DeploymentProbeError(
                "native result contains an unexpected or duplicate operation"
            )
        observed_pairs.add(pair)
        observed_at = row.get("observedAt")
        observed_time = preflight._read_timestamp(
            observed_at, f"{provider} {operation_id} native observation"
        )
        if observed_time > verified_datetime:
            raise DeploymentProbeError("native observation occurred after evidence verification")
        if row.get("outcome") == "verified" and isinstance(row.get("result"), dict):
            result = row["result"]
            digest = _sha256_bytes(_canonical_json(result))
            succeeded = result.get("isError") is not True
            failure = None
            operation = expected_by_pair[pair]
            if (
                provider == "runpod"
                and operation_id == "cloud.provider.health.read"
                and not _runpod_health_succeeded(result, operation)
            ):
                succeeded = False
                failure = {"errorType": "RUNPOD_HEALTH_INVALID"}
        elif row.get("outcome") == "failed":
            failure = row.get("failure")
            if not isinstance(failure, dict) or set(failure) - {
                "errorType",
                "httpStatus",
                "outcomeUnknown",
            }:
                raise DeploymentProbeError("native failure projection contains unsupported fields")
            digest = None
            succeeded = False
        else:
            raise DeploymentProbeError("native operation result has an unsupported outcome")
        all_success = all_success and succeeded
        summary_rows.append(
            {
                "providerId": provider,
                "operationId": operation_id,
                "outcome": "SUCCEEDED" if succeeded else "FAILED",
                "observedAt": observed_at,
                "resultDigestSha256": digest,
                "failure": failure,
            }
        )
    if observed_pairs != expected_pairs:
        raise DeploymentProbeError("native result set omitted a fixed read operation")
    summary_rows.sort(key=lambda row: (row["providerId"], row["operationId"]))
    return all_success, summary_rows, verified_at


def _notification_lineage(
    active: dict[str, Any],
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Read anchor/provenance files committed by the accepted active plan."""

    plan = active["plan"]
    inputs = plan.get("candidateInputs")
    paths = plan.get("candidateInputPaths")
    if not isinstance(inputs, dict) or not isinstance(paths, dict):
        raise DeploymentProbeError("accepted plan has no notification source records")
    anchor_record = inputs.get("notificationAnchor")
    provenance_record = inputs.get("notificationProvenance")
    anchor_path_text = paths.get("notificationAnchorFile")
    provenance_path_text = paths.get("notificationProvenanceFile")
    notification_inputs = plan.get("candidatePreflight", {}).get("notificationInputs")
    if (
        not isinstance(anchor_record, dict)
        or not isinstance(provenance_record, dict)
        or not isinstance(anchor_path_text, str)
        or not isinstance(provenance_path_text, str)
        or anchor_record.get("path") != anchor_path_text
        or provenance_record.get("path") != provenance_path_text
        or not isinstance(notification_inputs, dict)
        or notification_inputs.get("credentialNotificationAnchorSha256")
        != anchor_record.get("sha256")
        or notification_inputs.get("credentialNotificationProvenanceSha256")
        != provenance_record.get("sha256")
    ):
        raise DeploymentProbeError("accepted selector has no exact notification source lineage")
    anchor_path = Path(anchor_path_text)
    provenance_path = Path(provenance_path_text)
    stage_transition._verify_record(anchor_record, "accepted notification anchor")
    stage_transition._verify_record(provenance_record, "accepted notification provenance")
    for path, label in (
        (anchor_path, "accepted notification anchor"),
        (provenance_path, "accepted notification provenance"),
    ):
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not preflight._private_artifact(path)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != RUNTIME_OWNER_UID
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise DeploymentProbeError(f"{label} is not a private accepted source artifact")
    anchor, anchor_bytes = _read_json(
        anchor_path, "accepted notification anchor", maximum_bytes=1024 * 1024
    )
    old_provenance, old_provenance_bytes = _read_json(
        provenance_path, "accepted notification provenance", maximum_bytes=1024 * 1024
    )
    if (
        old_provenance.get("schemaVersion") != "cyrene-teams-credential-notifications-provenance-v1"
        or old_provenance.get("sourceAnchorSha256") != _sha256_bytes(anchor_bytes)
        or old_provenance.get("sourceTaskId") != anchor.get("taskId")
        or not isinstance(old_provenance.get("sourceWorkspaceCommit"), str)
        or re.fullmatch(r"[0-9a-f]{40,64}", old_provenance["sourceWorkspaceCommit"]) is None
        or _sha256_bytes(anchor_bytes) != anchor_record.get("sha256")
        or _sha256_bytes(old_provenance_bytes) != provenance_record.get("sha256")
    ):
        raise DeploymentProbeError("notification anchor/provenance source is not accepted lineage")
    return anchor_path, anchor, old_provenance


def _candidate_runpod_notification_config(
    *, active: dict[str, Any], candidate_config_dir: Path
) -> tuple[Path, dict[str, Any]]:
    """Accept a private notification candidate that adds only its issued RunPod binding."""

    candidate_dir = _validate_private_directory(
        candidate_config_dir, uid=RUNTIME_OWNER_UID, label="candidate notification config"
    )
    active_dir = Path(active["selector"]["configDir"])
    if candidate_dir == active_dir:
        raise DeploymentProbeError(
            "candidate notification config must be separate from active config"
        )
    active_path = active_dir / "teams-credential-notifications.json"
    candidate_path = candidate_dir / "teams-credential-notifications.json"
    active_notification, _active_bytes = _read_json(
        active_path, "active credential notification config", maximum_bytes=1024 * 1024
    )
    candidate_notification, _candidate_bytes = _read_json(
        candidate_path, "candidate credential notification config", maximum_bytes=1024 * 1024
    )
    active_base = {key: value for key, value in active_notification.items() if key != "connections"}
    candidate_base = {
        key: value for key, value in candidate_notification.items() if key != "connections"
    }
    if active_base != candidate_base:
        raise DeploymentProbeError(
            "candidate notification owner, workspace, route, and profile must match active config"
        )

    def _connections_by_provider(document: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
        rows = document.get("connections")
        if not isinstance(rows, list):
            raise DeploymentProbeError(f"{label} notification connections are malformed")
        mapped: dict[str, dict[str, Any]] = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != {"connectionId", "provider"}
                or not isinstance(row.get("provider"), str)
                or not isinstance(row.get("connectionId"), str)
                or row["provider"] in mapped
            ):
                raise DeploymentProbeError(f"{label} notification connections are malformed")
            mapped[row["provider"]] = row
        return mapped

    active_connections = _connections_by_provider(active_notification, "active")
    candidate_connections = _connections_by_provider(candidate_notification, "candidate")
    if "runpod" in active_connections or set(candidate_connections) != set(active_connections) | {
        "runpod"
    }:
        raise DeploymentProbeError(
            "candidate notification config must add exactly one RunPod connection"
        )
    if any(
        candidate_connections[provider] != active_connections[provider]
        for provider in active_connections
    ):
        raise DeploymentProbeError(
            "candidate notification config changed an existing provider route"
        )
    try:
        runpod_connection_id = str(uuid.UUID(candidate_connections["runpod"]["connectionId"]))
    except (ValueError, TypeError, AttributeError) as error:
        raise DeploymentProbeError(
            "candidate RunPod notification connection ID is malformed"
        ) from error
    if runpod_connection_id != candidate_connections["runpod"]["connectionId"]:
        raise DeploymentProbeError("candidate RunPod notification connection ID is noncanonical")

    active_binding_document = stage_transition._candidate_config_json(
        active["compose"], "connector-bindings"
    )
    active_bindings = stage_transition._sorted_exchange_bindings(
        active_binding_document.get("bindings")
    )
    candidate_binding_document, _binding_bytes = _read_json(
        candidate_dir / "connector-bindings.json",
        "candidate connector bindings",
        maximum_bytes=1024 * 1024,
    )
    candidate_bindings = stage_transition._sorted_exchange_bindings(
        candidate_binding_document.get("bindings")
    )
    active_non_runpod = [row for row in active_bindings if row["providerId"] != "runpod"]
    candidate_non_runpod = [row for row in candidate_bindings if row["providerId"] != "runpod"]
    candidate_runpod = [row for row in candidate_bindings if row["providerId"] == "runpod"]
    if any(row["providerId"] == "runpod" for row in active_bindings):
        raise DeploymentProbeError("active bindings already contain a RunPod connection")
    candidate_runpod_operations = (
        {tool["operationId"] for tool in candidate_runpod[0]["tools"]}
        if candidate_runpod
        else set()
    )
    if (
        len(candidate_runpod) != 1
        or active_non_runpod != candidate_non_runpod
        or candidate_runpod[0]["connectionRef"] != runpod_connection_id
        or not set(PROBE_OPERATIONS["runpod"]) <= candidate_runpod_operations
        or not candidate_runpod_operations
        <= preflight.CONNECTOR_MODEL_READONLY_OPERATIONS["runpod"]
        or any(tool["mutating"] is not False for tool in candidate_runpod[0]["tools"])
    ):
        raise DeploymentProbeError(
            "candidate bindings must preserve the active set and add its issued RunPod connection"
        )
    return candidate_path, candidate_notification


def _build_notification_provenance(
    *,
    active: dict[str, Any],
    anchor_path: Path,
    anchor: dict[str, Any],
    old_provenance: dict[str, Any],
    workspace_source_commit: str | None,
    candidate_config_dir: Path | None = None,
) -> dict[str, Any]:
    """Rebind the accepted source anchor to the selected notification configuration."""

    compose = active["compose"]
    if candidate_config_dir is None:
        config_dir = Path(active["selector"]["configDir"])
        notification_path = config_dir / "teams-credential-notifications.json"
        notification, _notification_bytes = _read_json(
            notification_path, "active credential notification config", maximum_bytes=1024 * 1024
        )
    else:
        config_dir = _validate_private_directory(
            candidate_config_dir, uid=RUNTIME_OWNER_UID, label="candidate notification config"
        )
        notification_path, notification = _candidate_runpod_notification_config(
            active=active, candidate_config_dir=config_dir
        )
    exchange_service = compose.get("services", {}).get("exchange", {})
    exchange_document = preflight._json_environment_object(
        exchange_service,
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON",
        "Exchange integration",
    )
    deployment_config = Path(preflight.__file__).resolve().parent / "config"
    notification_schema = deployment_config / "teams-credential-notifications.schema.json"
    source_workspace_commit = workspace_source_commit or old_provenance["sourceWorkspaceCommit"]
    if re.fullmatch(r"[0-9a-f]{40,64}", source_workspace_commit) is None:
        raise DeploymentProbeError("notification source Workspace commit is malformed")
    notification_exchange = notification.get("exchange")
    connector_host = exchange_document.get("connectorHost")
    teams_messages = (
        connector_host.get("teamsMessages") if isinstance(connector_host, dict) else None
    )
    if not isinstance(notification_exchange, dict) or not isinstance(teams_messages, dict):
        raise DeploymentProbeError("active notification Exchange profile is malformed")
    provenance = {
        "schemaVersion": "cyrene-teams-credential-notifications-provenance-v1",
        "sourceAnchorSha256": _sha256_bytes(anchor_path.read_bytes()),
        "sourceTaskId": old_provenance["sourceTaskId"],
        "candidateConfigSha256": _sha256_bytes(notification_path.read_bytes()),
        "candidateExchangeHostConfigSha256": preflight._canonical_json_digest(exchange_document),
        "schemaSha256": _sha256_bytes(notification_schema.read_bytes()),
        "ownerScopeSha256": preflight._canonical_json_digest(
            {
                "organizationId": notification.get("organizationId"),
                "workspaceId": notification.get("workspaceId"),
                "owner": notification.get("owner"),
            }
        ),
        "destinationSha256": preflight._canonical_json_digest(notification.get("destination")),
        "exchangeConnectionRef": teams_messages.get("connectionRef"),
        "exchangeProfileRef": notification_exchange.get("profileRef"),
        "sourceWorkspaceCommit": source_workspace_commit,
        "generatedAt": _utc_now(),
    }
    if (
        not isinstance(provenance["exchangeConnectionRef"], str)
        or not provenance["exchangeConnectionRef"]
    ):
        raise DeploymentProbeError("active Exchange notification connection is missing")
    return provenance


def _run_notification_validation(
    *,
    active: dict[str, Any],
    anchor_path: Path,
    provenance_path: Path,
    provider_ids: set[str],
    candidate_config_dir: Path | None = None,
) -> dict[str, str]:
    """Apply the official preflight notification validator to a fresh private receipt."""

    try:
        config_dir = (
            candidate_config_dir
            if candidate_config_dir is not None
            else Path(active["selector"]["configDir"])
        )
        compose = active["compose"]
        if candidate_config_dir is not None:
            compose = copy.deepcopy(compose)
            config_row = compose.get("configs", {}).get("teams-credential-notifications")
            if not isinstance(config_row, dict):
                raise DeploymentProbeError("candidate notification Compose config is unavailable")
            config_row["file"] = str(config_dir / "teams-credential-notifications.json")
        return preflight._validate_credential_notification_activation(
            compose,
            config_dir=config_dir,
            anchor_file=anchor_path,
            provenance_file=provenance_path,
            expected_provider_ids=provider_ids,
        )
    except preflight.PreflightError as error:
        raise DeploymentProbeError(
            "fresh notification provenance does not match accepted owner/configuration"
        ) from error


def _make_evidence(
    *,
    task_id: str,
    owner: str,
    verified_at: str,
    scope_digests: dict[str, str],
    auth_modes: dict[str, str],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the exact preflight-accepted Exchange connector probe envelope."""

    providers = set(scope_digests)
    if frozenset(providers) not in {
        REQUIRED_PROVIDERS,
        REQUIRED_PROVIDERS | OPTIONAL_PROVIDERS,
    }:
        raise DeploymentProbeError("evidence provider set is outside the closed probe contract")
    by_provider: dict[str, list[dict[str, Any]]] = {provider: [] for provider in providers}
    for row in rows:
        by_provider[row["providerId"]].append(row)
    return {
        "schemaVersion": "cyrene-connector-auth-readonly-evidence-v1",
        "status": "OPERATOR_ATTESTED",
        "evidenceClass": "OPERATOR_RECORDED_REAL_PROBE",
        "executionPath": "exchange_connector_api",
        "taskId": task_id,
        "owner": owner,
        "verifiedAt": verified_at,
        "providers": [
            {
                "providerId": provider,
                "identityStatus": "OPERATOR_RECORDED",
                "authMode": auth_modes[provider],
                "profileScopeDigestSha256": scope_digests[provider],
                "readOperations": [
                    {
                        "operationId": operation["operationId"],
                        "outcome": operation["outcome"],
                        "observedAt": operation["observedAt"],
                        "resultDigestSha256": operation["resultDigestSha256"],
                    }
                    for operation in by_provider[provider]
                ],
            }
            for provider in ("huggingface", "azure", "gcp", "runpod")
            if provider in providers
        ],
    }


def _public_summary(
    *,
    status: str,
    task_id: str,
    policy_sha256: str,
    active: dict[str, Any],
    readiness_path: Path | None,
    readiness_sha256: str | None,
    responses_path: Path | None,
    responses_sha256: str | None,
    evidence_path: Path | None,
    evidence_sha256: str | None,
    provenance_path: Path | None,
    provenance_sha256: str | None,
    summary_path: Path | None,
    summary_sha256: str | None,
    notification_anchor_path: Path | None = None,
) -> dict[str, Any]:
    """Return path/digest metadata only; never include provider responses or actor IDs."""

    return {
        "status": status,
        "taskId": task_id,
        "evidencePath": str(evidence_path) if evidence_path is not None else None,
        "evidenceSha256": evidence_sha256,
        "notificationAnchorPath": (
            str(notification_anchor_path) if notification_anchor_path is not None else None
        ),
        "provenancePath": str(provenance_path) if provenance_path is not None else None,
        "provenanceSha256": provenance_sha256,
        "readinessPath": str(readiness_path) if readiness_path is not None else None,
        "readinessSha256": readiness_sha256,
        "responsesPath": str(responses_path) if responses_path is not None else None,
        "responsesSha256": responses_sha256,
        "summaryPath": str(summary_path) if summary_path is not None else None,
        "summarySha256": summary_sha256,
        "activeOperationId": active["selector"]["operationId"],
        "activeImageLockSha256": active["selector"]["imageLock"]["sha256"],
        "policySha256": policy_sha256,
        "providerScopeDigests": active["scopeDigests"],
        "modelInvoked": False,
        "connectorWritesInvoked": False,
    }


def _run_deployment_probe_impl(
    policy_path: Path,
    *,
    run_id: str | None = None,
    workspace_source_commit: str | None = None,
    candidate_config_dir: Path | None = None,
) -> dict[str, Any]:
    """Run one fresh native read-only probe against the exact active selection.

    The function never changes runtime configuration, invokes a model, or retries
    a provider operation. Successful evidence is written only after all five
    operations pass and the selected runtime remains unchanged.

    中文：严格按活动 selector 派生 owner 与 scope，顺序调用五个只读操作；
    失败或状态不确定时不生成成功证据、不重放，也不改运行中配置。
    """

    if os.geteuid() != POLICY_OWNER_UID:
        raise DeploymentProbeError("deployment probe must run in its root-owned controller")
    policy, policy_sha256 = _load_policy(policy_path)
    runtime = policy["runtime"]
    output_dir = _validate_private_directory(
        Path(runtime["outputDirectory"]),
        uid=RUNTIME_OWNER_UID,
        label="probe output",
    )
    selected_run_id = run_id or uuid.uuid4().hex
    if re.fullmatch(r"[a-z0-9-]{1,48}", selected_run_id) is None:
        raise DeploymentProbeError("probe run ID is malformed")
    task_id = f"deployment-probe-{selected_run_id}"
    if (
        workspace_source_commit is not None
        and re.fullmatch(r"[0-9a-f]{40,64}", workspace_source_commit) is None
    ):
        raise DeploymentProbeError("Workspace source commit is malformed")
    paths = {
        "attempt": output_dir / f"deployment-probe-{selected_run_id}.started.private.json",
        "readiness": output_dir / f"deployment-probe-{selected_run_id}.readiness.private.json",
        "responses": output_dir / f"deployment-probe-{selected_run_id}.responses.private.json",
        "evidence": output_dir / f"connector-auth-readonly-evidence-{selected_run_id}.json",
        "provenance": output_dir
        / f"teams-credential-notifications-provenance-{selected_run_id}.json",
        "summary": output_dir / f"deployment-probe-{selected_run_id}.summary.private.json",
    }
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        raise DeploymentProbeError(
            "probe output or attempt marker already exists; choose a new run ID"
        )

    active = _load_active_runtime(policy)
    if candidate_config_dir is not None and not policy["notificationProvenanceRequired"]:
        raise DeploymentProbeError(
            "candidate notification config requires active notification provenance policy"
        )
    snapshot, readiness_source_sha256 = _read_exchange_readiness(active)
    operations, auth_modes = _build_probe_operations(
        snapshot=snapshot,
        compose=active["compose"],
        targets=policy["probeTargets"],
    )
    _check_policy_unchanged(policy_path, policy_sha256)
    selected_providers = sorted(active["scopeDigests"])
    _write_attempt_marker(
        output_dir,
        selected_run_id,
        policy_sha256,
        provider_ids=selected_providers,
        operation_count=sum(len(PROBE_OPERATIONS[provider]) for provider in selected_providers),
    )
    readiness_document = {
        "schemaVersion": READINESS_SCHEMA,
        "activeOperationId": active["selector"]["operationId"],
        "activeImageLockSha256": active["selector"]["imageLock"]["sha256"],
        "exchangeContainerId": active["services"]["exchange"]["containerId"],
        "exchangeImageId": active["services"]["exchange"]["imageId"],
        "snapshotSha256": readiness_source_sha256,
        "snapshot": snapshot,
        "readAt": _utc_now(),
    }
    readiness_file_sha256 = _write_private_json(paths["readiness"], readiness_document)

    raw_native: bytes | None = None
    responses_sha256: str | None = None
    evidence_sha256: str | None = None
    provenance_sha256: str | None = None
    evidence_path: Path | None = None
    provenance_path: Path | None = None
    notification_anchor_path: Path | None = None
    summary_path: Path | None = None
    summary_sha256: str | None = None
    try:
        native, raw_native = _execute_native_probe(
            active["services"]["navigator"]["containerId"],
            operations,
            active["actor"],
        )
        results_success, operation_rows, verified_at = _validate_native_results(native, operations)
        responses_sha256 = _write_private_json(
            paths["responses"],
            {
                "schemaVersion": RESPONSES_SCHEMA,
                "status": native.get("status"),
                "taskId": task_id,
                "activeOperationId": active["selector"]["operationId"],
                "activeImageLockSha256": active["selector"]["imageLock"]["sha256"],
                "readinessSha256": readiness_source_sha256,
                "providerScopeDigests": active["scopeDigests"],
                "results": native["results"],
            },
        )
        if not results_success:
            status = "NATIVE_EXCHANGE_READONLY_PROBE_FAILED"
            summary_sha256 = _write_private_json(
                paths["summary"],
                {
                    "schemaVersion": "cyrene-teams-deployment-probe-summary-v1",
                    "status": status,
                    "taskId": task_id,
                    "policySha256": policy_sha256,
                    "activeOperationId": active["selector"]["operationId"],
                    "activeImageLockSha256": active["selector"]["imageLock"]["sha256"],
                    "readinessSha256": readiness_source_sha256,
                    "responsesSha256": responses_sha256,
                    "operationOutcomes": operation_rows,
                    "modelInvoked": False,
                    "connectorWritesInvoked": False,
                },
            )
            summary_path = paths["summary"]
            return _public_summary(
                status=status,
                task_id=task_id,
                policy_sha256=policy_sha256,
                active=active,
                readiness_path=paths["readiness"],
                readiness_sha256=readiness_file_sha256,
                responses_path=paths["responses"],
                responses_sha256=responses_sha256,
                evidence_path=None,
                evidence_sha256=None,
                provenance_path=None,
                provenance_sha256=None,
                summary_path=summary_path,
                summary_sha256=summary_sha256,
            )

        _assert_active_runtime_unchanged(policy_path, policy_sha256, active)
        if policy["notificationProvenanceRequired"]:
            anchor_path, anchor, old_provenance = _notification_lineage(active)
            provenance_document = _build_notification_provenance(
                active=active,
                anchor_path=anchor_path,
                anchor=anchor,
                old_provenance=old_provenance,
                workspace_source_commit=workspace_source_commit,
                candidate_config_dir=candidate_config_dir,
            )
            provenance_sha256 = _write_private_json(paths["provenance"], provenance_document)
            try:
                _run_notification_validation(
                    active=active,
                    anchor_path=anchor_path,
                    provenance_path=paths["provenance"],
                    provider_ids=(
                        set(active["scopeDigests"]) | {"runpod"}
                        if candidate_config_dir is not None
                        else set(active["scopeDigests"])
                    ),
                    candidate_config_dir=candidate_config_dir,
                )
            except BaseException:
                paths["provenance"].unlink(missing_ok=True)
                raise
            provenance_path = paths["provenance"]
            notification_anchor_path = anchor_path

        evidence_document = _make_evidence(
            task_id=task_id,
            owner=active["actor"]["subject"],
            verified_at=verified_at,
            scope_digests=active["scopeDigests"],
            auth_modes=auth_modes,
            rows=operation_rows,
        )
        evidence_sha256 = _write_private_json(paths["evidence"], evidence_document)
        try:
            preflight._validate_connector_auth_readonly_evidence(
                paths["evidence"],
                expected_task_id=task_id,
                expected_owner=active["actor"]["subject"],
                expected_scope_digests=active["scopeDigests"],
            )
        except BaseException:
            paths["evidence"].unlink(missing_ok=True)
            if provenance_path is not None:
                provenance_path.unlink(missing_ok=True)
                provenance_path = None
                provenance_sha256 = None
            raise
        _assert_active_runtime_unchanged(policy_path, policy_sha256, active)
        evidence_path = paths["evidence"]
        status = "REAL_NATIVE_EXCHANGE_READONLY_PROBE_PASS"
        summary_sha256 = _write_private_json(
            paths["summary"],
            {
                "schemaVersion": "cyrene-teams-deployment-probe-summary-v1",
                "status": status,
                "taskId": task_id,
                "policySha256": policy_sha256,
                "activeOperationId": active["selector"]["operationId"],
                "activeImageLockSha256": active["selector"]["imageLock"]["sha256"],
                "readinessSha256": readiness_source_sha256,
                "responsesSha256": responses_sha256,
                "evidenceSha256": evidence_sha256,
                "notificationAnchorPath": (
                    str(notification_anchor_path) if notification_anchor_path is not None else None
                ),
                "provenanceSha256": provenance_sha256,
                "operationOutcomes": operation_rows,
                "modelInvoked": False,
                "connectorWritesInvoked": False,
            },
        )
        summary_path = paths["summary"]
        return _public_summary(
            status=status,
            task_id=task_id,
            policy_sha256=policy_sha256,
            active=active,
            readiness_path=paths["readiness"],
            readiness_sha256=readiness_file_sha256,
            responses_path=paths["responses"],
            responses_sha256=responses_sha256,
            evidence_path=evidence_path,
            evidence_sha256=evidence_sha256,
            provenance_path=provenance_path,
            provenance_sha256=provenance_sha256,
            summary_path=summary_path,
            summary_sha256=summary_sha256,
            notification_anchor_path=notification_anchor_path,
        )
    except (
        OSError,
        DeploymentProbeError,
        preflight.PreflightError,
        runtime_update.RuntimeUpdateError,
        stage_transition.StageTransitionError,
        subprocess.SubprocessError,
        AttributeError,
        ValueError,
        TypeError,
        KeyError,
    ) as error:
        status = "NATIVE_EXCHANGE_PROBE_BLOCKED"
        if raw_native is not None and not paths["responses"].exists():
            responses_sha256 = _write_private_json(
                paths["responses"],
                {
                    "schemaVersion": RESPONSES_SCHEMA,
                    "status": "UNKNOWN",
                    "taskId": task_id,
                    "activeOperationId": active["selector"]["operationId"],
                    "activeImageLockSha256": active["selector"]["imageLock"]["sha256"],
                    "readinessSha256": readiness_source_sha256,
                    "rawOutputBase64": base64.b64encode(raw_native).decode("ascii"),
                },
            )
        if provenance_path is not None and provenance_path.exists():
            provenance_path.unlink()
            provenance_path = None
            provenance_sha256 = None
        notification_anchor_path = None
        if evidence_path is not None and evidence_path.exists():
            evidence_path.unlink()
            evidence_path = None
            evidence_sha256 = None
        summary_doc = {
            "schemaVersion": "cyrene-teams-deployment-probe-summary-v1",
            "status": status,
            "taskId": task_id,
            "errorType": type(error).__name__,
            "policySha256": policy_sha256,
            "activeOperationId": active["selector"]["operationId"],
            "activeImageLockSha256": active["selector"]["imageLock"]["sha256"],
            "readinessSha256": readiness_source_sha256,
            "responsesSha256": responses_sha256,
            "modelInvoked": False,
            "connectorWritesInvoked": False,
        }
        if not paths["summary"].exists():
            summary_sha256 = _write_private_json(paths["summary"], summary_doc)
            summary_path = paths["summary"]
        return _public_summary(
            status=status,
            task_id=task_id,
            policy_sha256=policy_sha256,
            active=active,
            readiness_path=paths["readiness"],
            readiness_sha256=readiness_file_sha256,
            responses_path=paths["responses"] if paths["responses"].exists() else None,
            responses_sha256=responses_sha256,
            evidence_path=evidence_path,
            evidence_sha256=evidence_sha256,
            provenance_path=provenance_path,
            provenance_sha256=provenance_sha256,
            summary_path=summary_path,
            summary_sha256=summary_sha256,
            notification_anchor_path=notification_anchor_path,
        )


def run_deployment_probe(
    policy_path: Path,
    *,
    run_id: str | None = None,
    workspace_source_commit: str | None = None,
    candidate_config_dir: Path | None = None,
) -> dict[str, Any]:
    """Run one probe and convert unexpected runtime faults to a sanitized hold."""

    try:
        return _run_deployment_probe_impl(
            policy_path,
            run_id=run_id,
            workspace_source_commit=workspace_source_commit,
            candidate_config_dir=candidate_config_dir,
        )
    except DeploymentProbeError:
        raise
    except Exception as error:
        raise DeploymentProbeError("deployment probe could not validate or complete") from error


def _run_github_read_acceptance_impl(
    policy_path: Path,
    *,
    run_id: str | None,
) -> dict[str, Any]:
    """Execute the separate ten-connection GitHub repository-read acceptance."""

    if os.geteuid() != POLICY_OWNER_UID:
        raise DeploymentProbeError("GitHub acceptance must run in its root-owned controller")
    policy, policy_sha256 = _load_policy(policy_path)
    output_dir = _validate_private_directory(
        Path(policy["runtime"]["outputDirectory"]),
        uid=RUNTIME_OWNER_UID,
        label="GitHub acceptance output",
    )
    selected_run_id = run_id or uuid.uuid4().hex
    if re.fullmatch(r"[a-z0-9-]{1,48}", selected_run_id) is None:
        raise DeploymentProbeError("GitHub acceptance run ID is malformed")
    task_id = f"github-read-acceptance-{selected_run_id}"
    paths = {
        "attempt": output_dir / f"github-read-acceptance-{selected_run_id}.started.private.json",
        "readiness": output_dir
        / f"github-read-acceptance-{selected_run_id}.readiness.private.json",
        "responses": output_dir
        / f"github-read-acceptance-{selected_run_id}.responses.private.json",
        "evidence": output_dir / f"github-read-acceptance-{selected_run_id}.evidence.private.json",
        "summary": output_dir / f"github-read-acceptance-{selected_run_id}.summary.private.json",
    }
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        raise DeploymentProbeError("GitHub acceptance output exists; choose a new run ID")

    active = _load_active_runtime(policy)
    snapshot, readiness_source_sha256 = _read_exchange_readiness(active)
    operations = _build_github_read_operations(snapshot)
    _check_policy_unchanged(policy_path, policy_sha256)
    _write_attempt_marker(
        output_dir,
        selected_run_id,
        policy_sha256,
        name_prefix="github-read-acceptance",
        provider_ids=["github"],
        operation_count=len(operations),
    )
    readiness_sha256 = _write_private_json(
        paths["readiness"],
        {
            "schemaVersion": READINESS_SCHEMA,
            "activeOperationId": active["selector"]["operationId"],
            "activeImageLockSha256": active["selector"]["imageLock"]["sha256"],
            "exchangeContainerId": active["services"]["exchange"]["containerId"],
            "exchangeImageId": active["services"]["exchange"]["imageId"],
            "snapshotSha256": readiness_source_sha256,
            "snapshot": snapshot,
            "readAt": _utc_now(),
        },
    )
    native, raw_native = _execute_native_probe(
        active["services"]["navigator"]["containerId"],
        operations,
        active["actor"],
        include_connection_ref=True,
        timeout_seconds=MAX_GITHUB_ACCEPTANCE_SECONDS,
    )
    raw_response_sha256 = _sha256_bytes(raw_native)
    responses_sha256 = _write_private_json(
        paths["responses"],
        {
            "schemaVersion": GITHUB_RESPONSES_SCHEMA,
            "taskId": task_id,
            "activeOperationId": active["selector"]["operationId"],
            "activeImageLockSha256": active["selector"]["imageLock"]["sha256"],
            "readinessSha256": readiness_source_sha256,
            "rawResponseSha256": raw_response_sha256,
            "rawResponseBase64": base64.b64encode(raw_native).decode("ascii"),
        },
    )
    validation_failure_code: str | None = None
    try:
        succeeded, operation_outcomes, verified_at = _validate_github_native_results(
            native, operations
        )
    except DeploymentProbeError:
        succeeded = False
        operation_outcomes = []
        verified_at = _utc_now()
        validation_failure_code = "GITHUB_RESULT_SET_INVALID"
    runtime_unchanged = True
    try:
        _assert_active_runtime_unchanged(policy_path, policy_sha256, active)
    except DeploymentProbeError:
        runtime_unchanged = False
        succeeded = False
        validation_failure_code = "ACTIVE_RUNTIME_CHANGED"
    if validation_failure_code == "ACTIVE_RUNTIME_CHANGED":
        status = "NATIVE_GITHUB_READONLY_ACCEPTANCE_BLOCKED"
    else:
        status = (
            "REAL_NATIVE_GITHUB_READONLY_PROBE_PASS"
            if succeeded
            else "NATIVE_GITHUB_READONLY_ACCEPTANCE_FAILED"
        )
    evidence_sha256 = _write_private_json(
        paths["evidence"],
        {
            "schemaVersion": GITHUB_ACCEPTANCE_SCHEMA,
            "status": status,
            "taskId": task_id,
            "activeOperationId": active["selector"]["operationId"],
            "activeImageLockSha256": active["selector"]["imageLock"]["sha256"],
            "readinessSha256": readiness_source_sha256,
            "rawResponseSha256": raw_response_sha256,
            "connectionCount": len(operations),
            "operationId": GITHUB_READ_OPERATION_ID,
            "verifiedAt": verified_at,
            "runtimeUnchanged": runtime_unchanged,
            "validationFailureCode": validation_failure_code,
            "results": operation_outcomes,
            "modelInvoked": False,
            "connectorWritesInvoked": False,
        },
    )
    summary_sha256 = _write_private_json(
        paths["summary"],
        {
            "schemaVersion": "cyrene-github-readonly-acceptance-summary-v1",
            "status": status,
            "taskId": task_id,
            "policySha256": policy_sha256,
            "activeOperationId": active["selector"]["operationId"],
            "activeImageLockSha256": active["selector"]["imageLock"]["sha256"],
            "connectionCount": len(operations),
            "readinessSha256": readiness_sha256,
            "rawResponseSha256": raw_response_sha256,
            "responsesSha256": responses_sha256,
            "evidenceSha256": evidence_sha256,
            "validationFailureCode": validation_failure_code,
            "results": operation_outcomes,
            "modelInvoked": False,
            "connectorWritesInvoked": False,
        },
    )
    return {
        "status": status,
        "taskId": task_id,
        "connectionCount": len(operations),
        "evidencePath": str(paths["evidence"]),
        "evidenceSha256": evidence_sha256,
        "responsesPath": str(paths["responses"]),
        "responsesSha256": responses_sha256,
        "rawResponseSha256": raw_response_sha256,
        "readinessPath": str(paths["readiness"]),
        "readinessSha256": readiness_sha256,
        "summaryPath": str(paths["summary"]),
        "summarySha256": summary_sha256,
        "activeOperationId": active["selector"]["operationId"],
        "activeImageLockSha256": active["selector"]["imageLock"]["sha256"],
        "modelInvoked": False,
        "connectorWritesInvoked": False,
    }


def run_github_read_acceptance(
    policy_path: Path,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Verify all ten current GitHub bindings with native, read-only repository calls."""

    try:
        return _run_github_read_acceptance_impl(policy_path, run_id=run_id)
    except DeploymentProbeError:
        raise
    except Exception as error:
        raise DeploymentProbeError(
            "GitHub read acceptance could not validate or complete"
        ) from error


def _assert_active_runtime_unchanged(
    policy_path: Path, policy_sha256: str, active: dict[str, Any]
) -> None:
    """Require selector, source manifests, and live service identity to stay pinned."""

    _check_policy_unchanged(policy_path, policy_sha256)
    if active["selectionPath"].read_bytes() != active["selectorBytes"]:
        raise DeploymentProbeError("active runtime selector changed during the probe")
    stage_transition._verify_all_inputs(active["plan"])
    current_services = stage_transition._live_service_inventory(
        compose=active["compose"],
        image_lock=active["imageLock"],
        docker_prefix=DOCKER_PREFIX,
        mount_identity_by_service=active["plan"]["runtimeConfigIdentity"]["candidate"],
    )
    if current_services != active["services"]:
        raise DeploymentProbeError("active runtime service identity changed during the probe")


def _health_window_sql_script() -> str:
    """Return the Exchange-owned read-only query for selected provider bindings."""

    return """import json, sqlite3, sys
from datetime import datetime, timedelta, timezone

database_path, selected_text, issuer, subject, organization_id, workspace_id, lead_text = sys.argv[1:8]
selected = json.loads(selected_text)
lead = int(lead_text)
now = datetime.now(timezone.utc)
stored_provider = {"github": "github", "huggingface": "hf", "azure": "azure", "gcp": "gcp"}

def utc(value, label):
    if not isinstance(value, str) or not value:
        raise RuntimeError(label + " is missing")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise RuntimeError(label + " is not UTC")
    return parsed.astimezone(timezone.utc)

if not isinstance(selected, list) or not selected:
    raise RuntimeError("selected health targets are malformed")
db = sqlite3.connect("file:" + database_path + "?mode=ro", uri=True, timeout=5)
try:
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    monitor_count = 0
    active_leases = 0
    inflight_calls = 0
    check_rows = []
    seen = set()
    for item in selected:
        if not isinstance(item, dict) or set(item) != {"providerId", "connectionRef"}:
            raise RuntimeError("selected connection row is malformed")
        connection_ref = item["connectionRef"]
        provider = item["providerId"]
        if provider not in stored_provider or not isinstance(connection_ref, str) or not connection_ref:
            raise RuntimeError("selected provider is unsupported")
        if connection_ref in seen:
            raise RuntimeError("selected connection is duplicated")
        seen.add(connection_ref)
        connection_rows = db.execute(
            "SELECT document FROM integration_connections "
            "WHERE id=? AND organization_id=? AND workspace_id=? "
            "AND actor_issuer=? AND actor_subject=? AND enabled=1",
            (connection_ref, organization_id, workspace_id, issuer, subject),
        ).fetchall()
        if len(connection_rows) != 1:
            raise RuntimeError("selected connection is missing or ambiguous")
        connection = json.loads(str(connection_rows[0][0]))
        if (
            not isinstance(connection, dict)
            or connection.get("id") != connection_ref
            or connection.get("provider") != stored_provider[provider]
            or connection.get("enabled") is not True
            or connection.get("organizationId") != organization_id
            or connection.get("workspaceId") != workspace_id
            or connection.get("actorIssuer") != issuer
            or connection.get("actorSubject") != subject
        ):
            raise RuntimeError("selected connection is outside its exact actor scope")
        monitor_rows = db.execute(
            "SELECT next_check_at, claim_owner, lease_expires_at "
            "FROM authorization_monitor_state WHERE connection_id=?",
            (connection_ref,),
        ).fetchall()
        if len(monitor_rows) != 1:
            raise RuntimeError("selected authorization monitor state is missing or ambiguous")
        next_check_text, claim_owner, lease_text = monitor_rows[0]
        next_check = utc(next_check_text, "next_check_at")
        if claim_owner is not None and (not isinstance(claim_owner, str) or not claim_owner):
            raise RuntimeError("monitor claim owner is malformed")
        if (claim_owner is None) != (lease_text is None):
            raise RuntimeError("monitor lease fields are inconsistent")
        if lease_text is not None:
            active_leases += int(utc(lease_text, "lease_expires_at") > now)
        inflight_calls += int(db.execute(
            "SELECT COUNT(*) FROM connector_invocations "
            "WHERE organization_id=? AND workspace_id=? AND actor_issuer=? "
            "AND actor_subject=? AND connection_ref=? AND provider_id=? "
            "AND state NOT IN ('SUCCEEDED','FAILED','REJECTED','RATE_LIMITED')",
            (organization_id, workspace_id, issuer, subject, connection_ref, provider),
        ).fetchone()[0])
        check_rows.append(next_check)
        monitor_count += 1
    earliest = min(check_rows)
    report = {
        "selectedConnectionCount": len(selected),
        "monitorStateCount": monitor_count,
        "activeMonitorLeaseCount": active_leases,
        "inFlightConnectorCallCount": inflight_calls,
        "nextCheckAt": earliest.isoformat().replace("+00:00", "Z"),
        "snapshotAtUtc": now.isoformat().replace("+00:00", "Z"),
        "nextCheckSecondsRemaining": int((earliest - now).total_seconds()),
        "minimumLeadSeconds": lead,
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
finally:
    db.close()
"""


def _select_health_connections(
    bindings: Any, targets: list[str]
) -> tuple[list[dict[str, str]], list[str]]:
    """Expand supported service targets to their exact active provider connections."""

    if (
        not isinstance(targets, list)
        or not targets
        or any(
            not isinstance(target, str) or target not in HEALTH_TARGET_PROVIDERS
            for target in targets
        )
        or len(set(targets)) != len(targets)
    ):
        raise DeploymentProbeError("health targets must be a unique supported service-name list")
    if not isinstance(bindings, list):
        raise DeploymentProbeError("active connector binding inventory is malformed")
    try:
        normalized = stage_transition._sorted_exchange_bindings(bindings)
    except stage_transition.StageTransitionError as error:
        raise DeploymentProbeError("active connector binding inventory is malformed") from error
    providers = set().union(*(HEALTH_TARGET_PROVIDERS[target] for target in targets))
    if (
        any(binding.get("providerId") == "runpod" for binding in normalized)
        and "runpod" not in providers
    ):
        raise DeploymentProbeError(
            "active RunPod bindings require an explicit RunPod health target"
        )
    refs_by_provider: dict[str, list[str]] = {provider: [] for provider in providers}
    for binding in normalized:
        provider = binding.get("providerId")
        if provider not in providers:
            continue
        connection_ref = binding.get("connectionRef")
        try:
            canonical_ref = str(uuid.UUID(connection_ref))
        except (ValueError, TypeError, AttributeError) as error:
            raise DeploymentProbeError(
                "active health binding has a malformed connection reference"
            ) from error
        if canonical_ref != connection_ref:
            raise DeploymentProbeError("active health connection reference is not canonical")
        refs_by_provider[provider].append(canonical_ref)
    for target in targets:
        for provider in HEALTH_TARGET_PROVIDERS[target]:
            refs = refs_by_provider.get(provider, [])
            if not refs or len(set(refs)) != len(refs) or (provider != "github" and len(refs) != 1):
                raise DeploymentProbeError(
                    f"active {provider} health bindings are missing or ambiguous"
                )
    selected = [
        {"providerId": provider, "connectionRef": reference}
        for provider in sorted(providers)
        for reference in sorted(refs_by_provider[provider])
    ]
    if not selected:
        raise DeploymentProbeError("selected health target set has no connection references")
    return selected, [row["connectionRef"] for row in selected]


def _readonly_health_window_impl(
    policy_path: Path,
    targets: list[str],
    minimum_lead_seconds: int,
) -> dict[str, Any]:
    """Read selected provider monitor metadata once without rescheduling it.

    Targets are the closed service names github-issues, huggingface,
    cloud-management-phase2, and runpod. Their current connection references are derived
    from the active binding config and then checked against Exchange's read-only
    connection and monitor tables.

    中文：按活动 binding 派生连接 ID，只读检查监视租约、进行中调用与下次检查窗口；
    不修改 Exchange 数据库、不启动额外 poller，也不改变调度时间。
    """

    if os.geteuid() != POLICY_OWNER_UID:
        raise DeploymentProbeError("health-window read must run in its root-owned controller")
    if (
        isinstance(minimum_lead_seconds, bool)
        or not isinstance(minimum_lead_seconds, int)
        or not 1 <= minimum_lead_seconds <= MAX_HEALTH_WINDOW_SECONDS
    ):
        raise DeploymentProbeError("minimum lead time is outside the bounded health window")
    policy, policy_sha256 = _load_policy(policy_path)
    active = _load_active_runtime(policy)
    binding_document = stage_transition._candidate_config_json(
        active["compose"], "connector-bindings"
    )
    selected, ordered_refs = _select_health_connections(binding_document.get("bindings"), targets)
    exchange = active["services"].get("exchange")
    if not isinstance(exchange, dict) or not isinstance(exchange.get("containerId"), str):
        raise DeploymentProbeError("active Exchange service is missing for health-window read")
    actor = active["actor"]
    selected_text = json.dumps(selected, sort_keys=True, separators=(",", ":"))
    python = str(Path(stage_transition.EXCHANGE_READINESS_EXECUTABLE).with_name("python"))
    command = [
        *DOCKER_PREFIX,
        "exec",
        "--user",
        f"{RUNTIME_OWNER_UID}:{RUNTIME_OWNER_UID}",
        exchange["containerId"],
        python,
        "-c",
        _health_window_sql_script(),
        stage_transition.EXCHANGE_INTEGRATION_DATABASE,
        selected_text,
        actor["issuer"],
        actor["subject"],
        actor["organizationId"],
        actor["workspaceId"],
        str(minimum_lead_seconds),
    ]
    try:
        output = runtime_update._checked_stdout(
            command,
            label="read-only connector authorization health window",
            timeout_seconds=15,
        )
        report = runtime_update._decode_json_output(
            output, "read-only connector health-window report"
        )
    except (OSError, runtime_update.RuntimeUpdateError, subprocess.SubprocessError) as error:
        raise DeploymentProbeError(
            "connector authorization health window could not be read"
        ) from error
    required = {
        "selectedConnectionCount",
        "monitorStateCount",
        "activeMonitorLeaseCount",
        "inFlightConnectorCallCount",
        "nextCheckAt",
        "snapshotAtUtc",
        "nextCheckSecondsRemaining",
        "minimumLeadSeconds",
    }
    if (
        not isinstance(report, dict)
        or set(report) != required
        or any(
            isinstance(report.get(field), bool) or not isinstance(report.get(field), int)
            for field in (
                "selectedConnectionCount",
                "monitorStateCount",
                "activeMonitorLeaseCount",
                "inFlightConnectorCallCount",
                "nextCheckSecondsRemaining",
                "minimumLeadSeconds",
            )
        )
        or report["selectedConnectionCount"] != len(selected)
        or report["monitorStateCount"] != len(selected)
        or report["activeMonitorLeaseCount"] < 0
        or report["activeMonitorLeaseCount"] > len(selected)
        or report["inFlightConnectorCallCount"] < 0
        or report["minimumLeadSeconds"] != minimum_lead_seconds
        or not isinstance(report.get("nextCheckAt"), str)
        or not isinstance(report.get("snapshotAtUtc"), str)
    ):
        raise DeploymentProbeError("connector health-window report is malformed")
    next_check = _parse_utc_timestamp(report["nextCheckAt"], "authorization next check")
    snapshot_at = _parse_utc_timestamp(report["snapshotAtUtc"], "authorization health snapshot")
    now = datetime.now(UTC)
    if (
        abs(int((next_check - snapshot_at).total_seconds()) - report["nextCheckSecondsRemaining"])
        > 2
        or snapshot_at > now + timedelta(seconds=5)
        or now - snapshot_at > timedelta(seconds=120)
    ):
        raise DeploymentProbeError(
            "authorization health-window timestamps are inconsistent or stale"
        )
    ready = (
        report["activeMonitorLeaseCount"] == 0
        and report["inFlightConnectorCallCount"] == 0
        and report["nextCheckSecondsRemaining"] > minimum_lead_seconds
    )
    _assert_active_runtime_unchanged(policy_path, policy_sha256, active)
    return {
        "status": "READY" if ready else "BUSY",
        "ready": ready,
        "busy": not ready,
        "targets": sorted(targets),
        "selectedConnectionCount": len(selected),
        **report,
        "connectionRefsSha256": _sha256_bytes(
            _canonical_json(sorted(_sha256_bytes(ref.encode("utf-8")) for ref in ordered_refs))
        ),
        "checkedAtUtc": _utc_now(),
        "modelInvoked": False,
        "connectorWritesInvoked": False,
    }


def readonly_health_window(
    policy_path: Path,
    targets: list[str],
    minimum_lead_seconds: int,
) -> dict[str, Any]:
    """Read the normal connector monitor window or return a sanitized hold."""

    try:
        return _readonly_health_window_impl(policy_path, targets, minimum_lead_seconds)
    except DeploymentProbeError:
        raise
    except Exception as error:
        raise DeploymentProbeError("authorization health window could not be validated") from error


def main() -> int:
    """Run one command-line probe and print only a sanitized receipt summary."""

    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--workspace-source-commit")
    parser.add_argument("--candidate-config-dir", type=Path)
    args = parser.parse_args()
    try:
        result = run_deployment_probe(
            args.policy,
            run_id=args.run_id,
            workspace_source_commit=args.workspace_source_commit,
            candidate_config_dir=args.candidate_config_dir,
        )
    except (
        OSError,
        DeploymentProbeError,
        preflight.PreflightError,
        runtime_update.RuntimeUpdateError,
        stage_transition.StageTransitionError,
        subprocess.SubprocessError,
        AttributeError,
        ValueError,
        TypeError,
        KeyError,
    ) as error:
        print(
            json.dumps(
                {"status": "DEPLOYMENT_PROBE_BLOCKED", "errorType": type(error).__name__},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "REAL_NATIVE_EXCHANGE_READONLY_PROBE_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
