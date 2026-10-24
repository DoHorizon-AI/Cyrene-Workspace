"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 api_acceptance_budget.py                                             │
│  Module: tooling.deploy.teams_cloud.api_acceptance_budget               │
│  Role: Persist bounded reservations around the existing DSH API path.   │
│                                                                          │
│  模块职责：围绕既有 DSH API 路径持久化预算预留和写入恢复状态。          │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

MAX_BUDGET_USD = 5.0
MAX_RESOURCE_LIFETIME_SECONDS = 900
CLEANUP_RESERVE_SECONDS = 300
MAX_PRICE_AGE = timedelta(hours=24)
TASK_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,43}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SENSITIVE_TEXT_RE = re.compile(
    r"(password|passwd|token|secret|private.?key|client.?secret|api.?key|access.?key|credential|authorization)",
    re.IGNORECASE,
)
PROVIDER_BY_STAGE = {"azure_gcp": {"azure", "gcp"}, "runpod": {"runpod"}}
AUTH_MODES = {
    "azure": "managed_identity",
    "gcp": "workload_identity_federation",
    "runpod": "api_key",
}
WRITE_TOOL_IDS = {
    "cloud.resource.create": "cloud_resource_create",
    "cloud.resource.deploy": "cloud_resource_deploy",
    "cloud.resource.start": "cloud_resource_start",
    "cloud.resource.stop": "cloud_resource_stop",
    "cloud.resource.restart": "cloud_resource_restart",
    "cloud.resource.update": "cloud_resource_update",
    "cloud.resource.delete": "cloud_resource_delete",
    "cloud.operation.cancel": "cloud_operation_cancel",
}


class ApiBudgetError(ValueError):
    """A reservation, task identity, or write recovery invariant failed."""


def _now_utc(now: datetime | None = None) -> datetime:
    """Return one timezone-aware UTC clock value."""

    selected = now or datetime.now(UTC)
    if selected.tzinfo is None:
        raise ApiBudgetError("timestamps must include a timezone")
    return selected.astimezone(UTC)


def _timestamp(value: Any, label: str) -> datetime:
    """Parse one ISO timestamp without accepting naive local time."""

    if not isinstance(value, str) or not value:
        raise ApiBudgetError(f"{label} requires a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ApiBudgetError(f"{label} is not valid ISO-8601") from error
    if parsed.tzinfo is None:
        raise ApiBudgetError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    """Encode one timestamp in canonical UTC form."""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate keys before consuming a private receipt."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ApiBudgetError("private evidence contains duplicate JSON keys")
        result[key] = value
    return result


def _private_file(path: Path, *, label: str, must_exist: bool = True) -> None:
    """Keep writable ledgers and evidence outside source checkouts and owner-only."""

    resolved = path.resolve()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ApiBudgetError(f"{label} path must not traverse symlinks")
    if ".artifacts" not in resolved.parts or any(
        (parent / ".git").exists() for parent in (resolved, *resolved.parents)
    ):
        raise ApiBudgetError(f"{label} must remain under umbrella .artifacts")
    if path.is_symlink():
        raise ApiBudgetError(f"{label} must not be a symlink")
    if not must_exist:
        parent = path.parent
        if not parent.is_dir() or parent.is_symlink():
            raise ApiBudgetError(f"{label} parent directory must already exist")
        if parent.stat().st_uid != os.geteuid() or parent.stat().st_mode & 0o077:
            raise ApiBudgetError(f"{label} parent directory must be owner-only")
        return
    if not path.is_file():
        raise ApiBudgetError(f"{label} is unavailable")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ApiBudgetError(f"{label} must be owner-only and owned by the current service user")


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    """Read one private JSON receipt and preserve its exact bytes for hash chaining."""

    _private_file(path, label=label)
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ApiBudgetError(f"{label} exceeds the accepted evidence size")
    try:
        document = json.loads(raw, object_pairs_hook=_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ApiBudgetError(f"{label} is invalid JSON") from error
    if not isinstance(document, dict):
        raise ApiBudgetError(f"{label} must be a JSON object")
    return document, raw


def _validate_previous_github_hf(path: Path, *, task_id: str, owner: str) -> tuple[bytes, float]:
    """Require a same-task real GitHub/HF receipt before any cloud reservation."""

    evidence, raw = _read_json(path, "GitHub/Hugging Face API evidence")
    required = {
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
    allowed = required | {"$schema"}
    providers = evidence.get("providers")
    if (
        set(evidence) - allowed
        or not required <= set(evidence)
        or evidence.get("schemaVersion") != "cyrene-cloud-acceptance-evidence-v1"
        or evidence.get("stage") != "github_hf"
        or evidence.get("status") != "VERIFIED"
        or evidence.get("evidenceClass") != "REAL_PROVIDER_API"
        or evidence.get("taskId") != task_id
        or evidence.get("owner") != owner
        or evidence.get("reservedUsd") != 0
        or evidence.get("cumulativeReservedUsd") != 0
        or not isinstance(providers, dict)
        or set(providers) != {"github", "huggingface"}
    ):
        raise ApiBudgetError("cloud reservation requires verified same-task GitHub/HF API evidence")
    for provider, result in providers.items():
        if (
            not isinstance(result, dict)
            or result.get("identityStatus") != "VERIFIED"
            or result.get("privateReadStatus") != "VERIFIED"
            or result.get("writeReadbackStatus") != "VERIFIED"
            or result.get("cleanupReadbackStatus") != "ABSENT"
            or not isinstance(result.get("resourceRef"), str)
            or not result["resourceRef"]
            or not isinstance(result.get("writeReadbackSha256"), str)
            or not SHA256_RE.fullmatch(result["writeReadbackSha256"])
        ):
            raise ApiBudgetError(
                f"{provider} prerequisite is missing API read-back or cleanup proof"
            )
    return raw, 0.0


def _validate_previous_azure_gcp(
    path: Path,
    *,
    task_id: str,
    owner: str,
    github_hf_raw: bytes,
) -> tuple[bytes, float]:
    """Require a verified Azure/GCP typed-API receipt for the RunPod stage."""

    evidence, raw = _read_json(path, "Azure/GCP provider API evidence")
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
        or evidence.get("taskId") != task_id
        or evidence.get("owner") != owner
        or evidence.get("stage") != "azure_gcp"
        or evidence.get("status") != "VERIFIED"
        or evidence.get("evidenceClass") != "REAL_PROVIDER_API"
        or evidence.get("executionPath") != "navigator_dsh_exchange_directplugin_api"
        or evidence.get("previousEvidenceSha256") != hashlib.sha256(github_hf_raw).hexdigest()
        or not isinstance(reserved, (int, float))
        or isinstance(reserved, bool)
        or not math.isfinite(reserved)
        or reserved < 0
        or not isinstance(cumulative, (int, float))
        or isinstance(cumulative, bool)
        or not math.isfinite(cumulative)
        or cumulative < reserved
        or cumulative > MAX_BUDGET_USD
        or not isinstance(providers, list)
        or {row.get("providerId") for row in providers if isinstance(row, dict)} != {"azure", "gcp"}
        or len(providers) != 2
    ):
        raise ApiBudgetError(
            "RunPod reservation requires a verified same-task Azure/GCP API receipt"
        )
    _timestamp(evidence.get("verifiedAt"), "Azure/GCP API verification")
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
        provider_id = provider.get("providerId") if isinstance(provider, dict) else None
        if (
            not isinstance(provider, dict)
            or set(provider) != provider_fields
            or provider.get("identityStatus") != "VERIFIED"
            or provider.get("authMode") != AUTH_MODES.get(provider_id)
            or not isinstance(provider.get("profileScopeDigestSha256"), str)
            or not SHA256_RE.fullmatch(provider["profileScopeDigestSha256"])
            or not isinstance(provider.get("resources"), list)
            or not provider["resources"]
            or not isinstance(provider.get("writeOperations"), list)
            or not provider["writeOperations"]
        ):
            raise ApiBudgetError("Azure/GCP receipt provider identity or scope is malformed")
        for resource in provider["resources"]:
            ownership = resource.get("ownership") if isinstance(resource, dict) else None
            if (
                not isinstance(resource, dict)
                or set(resource) != resource_fields
                or not isinstance(ownership, dict)
                or set(ownership) != {"taskId", "owner", "marker"}
                or ownership.get("taskId") != task_id
                or ownership.get("owner") != owner
                or not isinstance(ownership.get("marker"), str)
                or task_id not in ownership["marker"]
                or not isinstance(resource.get("vendorRef"), str)
                or not resource["vendorRef"]
                or resource.get("cleanupAbsenceStatus") != "ABSENT"
            ):
                raise ApiBudgetError("Azure/GCP receipt contains an unowned or uncleared resource")
            _timestamp(resource.get("cleanupReadbackAt"), "Azure/GCP cleanup read-back")
        for operation in provider["writeOperations"]:
            state = operation.get("stateReadback") if isinstance(operation, dict) else None
            if (
                not isinstance(operation, dict)
                or set(operation) != operation_fields
                or operation.get("outcome") != "SUCCEEDED"
                or not isinstance(state, dict)
                or set(state) != {"status", "observedAt", "stateDigestSha256"}
                or state.get("status") != "VERIFIED"
                or not SHA256_RE.fullmatch(str(state.get("stateDigestSha256", "")))
                or not SHA256_RE.fullmatch(str(operation.get("requestDigestSha256", "")))
                or not SHA256_RE.fullmatch(str(operation.get("resultDigestSha256", "")))
                or not all(
                    isinstance(operation.get(field), str) and operation[field]
                    for field in (
                        "toolId",
                        "operationId",
                        "resourceRef",
                        "grantRef",
                        "workOperationId",
                        "operationRef",
                    )
                )
            ):
                raise ApiBudgetError(
                    "Azure/GCP receipt contains a write without verified API read-back"
                )
            _timestamp(state.get("observedAt"), "Azure/GCP operation read-back")
    return raw, float(cumulative)


class ApiAcceptanceLedger:
    """Reserve before calling the existing DSH tool path; never dispatch a provider itself."""

    def __init__(
        self,
        ledger_path: Path,
        *,
        task_id: str,
        owner: str,
        stage: str,
        previous_evidence: Path,
        github_hf_evidence: Path | None = None,
        now: datetime | None = None,
    ) -> None:
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise ApiBudgetError("task ID is malformed")
        if not isinstance(owner, str) or not UUID_RE.fullmatch(owner):
            raise ApiBudgetError("owner must be a verified AAD object UUID")
        owner = owner.lower()
        if stage not in PROVIDER_BY_STAGE:
            raise ApiBudgetError("API acceptance stage must be azure_gcp or runpod")
        _private_file(ledger_path, label="API reservation ledger", must_exist=False)
        self.path = ledger_path
        self.task_id = task_id
        self.owner = owner.lower()
        self.stage = stage
        self.github_hf_evidence_path = (
            github_hf_evidence.resolve()
            if github_hf_evidence is not None
            else previous_evidence.resolve()
        )
        self.previous_evidence_path = previous_evidence.resolve()
        if stage == "azure_gcp":
            self.previous_evidence_raw, self.previous_reserved_usd = _validate_previous_github_hf(
                previous_evidence, task_id=task_id, owner=owner
            )
            self.github_hf_evidence_raw = self.previous_evidence_raw
        else:
            if github_hf_evidence is None:
                raise ApiBudgetError("RunPod stage must retain the GitHub/HF chain root receipt")
            github_raw, _ = _validate_previous_github_hf(
                github_hf_evidence, task_id=task_id, owner=owner
            )
            self.github_hf_evidence_raw = github_raw
            self.previous_evidence_raw, self.previous_reserved_usd = _validate_previous_azure_gcp(
                previous_evidence,
                task_id=task_id,
                owner=owner,
                github_hf_raw=github_raw,
            )
        self.previous_evidence_sha256 = hashlib.sha256(self.previous_evidence_raw).hexdigest()
        self.now = _now_utc(now)
        self.fixed_now = now

    def _clock(self) -> datetime:
        return _now_utc(self.fixed_now)

    def _verify_predecessor_unchanged(self) -> None:
        """Refuse to continue if an input receipt changed after its chain was verified."""

        _document, previous_raw = _read_json(self.previous_evidence_path, "previous-stage evidence")
        if hashlib.sha256(previous_raw).hexdigest() != self.previous_evidence_sha256:
            raise ApiBudgetError(
                "previous-stage evidence changed after this ledger was initialized"
            )
        _document, root_raw = _read_json(
            self.github_hf_evidence_path, "GitHub/HF chain-root evidence"
        )
        if (
            hashlib.sha256(root_raw).hexdigest()
            != hashlib.sha256(self.github_hf_evidence_raw).hexdigest()
        ):
            raise ApiBudgetError("GitHub/HF chain-root evidence changed after validation")

    def _initial(self) -> dict[str, Any]:
        return {
            "schemaVersion": "cyrene-cloud-api-reservation-ledger-v1",
            "evidenceClass": "REAL_PROVIDER_API",
            "executionPath": "navigator_dsh_exchange_directplugin_api",
            "stage": self.stage,
            "status": "IN_PROGRESS",
            "taskId": self.task_id,
            "owner": self.owner,
            "budgetUsd": MAX_BUDGET_USD,
            "previousEvidenceSha256": self.previous_evidence_sha256,
            "previousReservedUsd": self.previous_reserved_usd,
            "reservedUsd": 0.0,
            "cumulativeReservedUsd": self.previous_reserved_usd,
            "resources": {},
            "events": [],
        }

    @contextlib.contextmanager
    def _locked_ledger(self):
        """Serialize every transition and replace the durable JSON atomically."""

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (
            self.path.parent.stat().st_uid != os.geteuid()
            or self.path.parent.stat().st_mode & 0o077
        ):
            raise ApiBudgetError("API ledger directory must be owner-only")
        lock_path = self.path.with_name(self.path.name + ".lock")
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                raise ApiBudgetError("API ledger lock has unsafe owner or permissions")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ApiBudgetError("another API acceptance transition is in progress") from error
            ledger = self._load_or_initialize()
            yield ledger
            self._atomic_write(self.path, ledger)
        finally:
            os.close(descriptor)

    def _load_or_initialize(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._initial()
        ledger, _raw = _read_json(self.path, "API reservation ledger")
        if (
            ledger.get("schemaVersion") != "cyrene-cloud-api-reservation-ledger-v1"
            or ledger.get("taskId") != self.task_id
            or ledger.get("owner") != self.owner
            or ledger.get("stage") != self.stage
            or ledger.get("budgetUsd") != MAX_BUDGET_USD
            or ledger.get("previousEvidenceSha256") != self.previous_evidence_sha256
            or ledger.get("previousReservedUsd") != self.previous_reserved_usd
            or not isinstance(ledger.get("resources"), dict)
            or not isinstance(ledger.get("events"), list)
            or ledger.get("status") not in {"IN_PROGRESS", "RECEIPT_PENDING", "VERIFIED"}
        ):
            raise ApiBudgetError("API ledger belongs to a different task, stage, or predecessor")
        _check_reservation_totals(ledger)
        return ledger

    def reserve_resource(
        self,
        *,
        resource_id: str,
        provider: str,
        marker: str,
        profile_scope_digest_sha256: str,
        target_digest_sha256: str,
        target_ref: str,
        quote_ref: str,
        quoted_at: str,
        upper_bound_usd: float,
        max_lifetime_seconds: int = MAX_RESOURCE_LIFETIME_SECONDS,
    ) -> None:
        """Durably reserve the full resource lifetime and cleanup cost before create."""

        if not isinstance(resource_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{2,80}", resource_id
        ):
            raise ApiBudgetError("resource ID must be a stable, bounded identifier")
        if provider not in PROVIDER_BY_STAGE[self.stage]:
            raise ApiBudgetError("provider is not enabled for this acceptance stage")
        if not isinstance(marker, str) or self.task_id not in marker or not 1 <= len(marker) <= 180:
            raise ApiBudgetError("resource ownership marker must include this task ID")
        if (
            not isinstance(target_ref, str)
            or not target_ref
            or len(target_ref) > 1024
            or SENSITIVE_TEXT_RE.search(target_ref)
            or self.task_id not in target_ref.lower()
        ):
            raise ApiBudgetError(
                "target reference must be a bounded task-marked non-secret resource name or ID"
            )
        for label, value in (
            ("profile scope digest", profile_scope_digest_sha256),
            ("target digest", target_digest_sha256),
        ):
            if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                raise ApiBudgetError(f"{label} must be a lowercase SHA-256 digest")
        parsed_quote = urlsplit(quote_ref) if isinstance(quote_ref, str) else None
        if (
            parsed_quote is None
            or parsed_quote.scheme != "https"
            or not parsed_quote.hostname
            or parsed_quote.username
            or parsed_quote.password
            or any(
                SENSITIVE_TEXT_RE.search(key)
                for key, _value in parse_qsl(parsed_quote.query, keep_blank_values=True)
            )
        ):
            raise ApiBudgetError("price quote must be a direct HTTPS reference")
        quote_time = _timestamp(quoted_at, "price quote")
        clock = self._clock()
        if quote_time > clock or clock - quote_time > MAX_PRICE_AGE:
            raise ApiBudgetError("price quote is future-dated or older than 24 hours")
        if (
            not isinstance(upper_bound_usd, (int, float))
            or isinstance(upper_bound_usd, bool)
            or not math.isfinite(upper_bound_usd)
            or upper_bound_usd < 0
        ):
            raise ApiBudgetError("resource quote must have a finite nonnegative USD upper bound")
        if (
            not isinstance(max_lifetime_seconds, int)
            or isinstance(max_lifetime_seconds, bool)
            or not 300 <= max_lifetime_seconds <= MAX_RESOURCE_LIFETIME_SECONDS
        ):
            raise ApiBudgetError("resource lifetime must be between 300 and 900 seconds")

        now = _now_utc()
        self._verify_predecessor_unchanged()
        with self._locked_ledger() as ledger:
            if ledger["status"] != "IN_PROGRESS":
                raise ApiBudgetError(
                    "a finalized API ledger cannot accept new resource reservations"
                )
            if resource_id in ledger["resources"]:
                raise ApiBudgetError(
                    "resource reservation already exists; reconcile instead of recreating"
                )
            existing_scope = [
                row for row in ledger["resources"].values() if row["provider"] == provider
            ]
            if any(
                row["profileScopeDigestSha256"] != profile_scope_digest_sha256
                for row in existing_scope
            ):
                raise ApiBudgetError("one provider stage must use a single fixed profile scope")
            reserved = float(ledger["reservedUsd"]) + float(upper_bound_usd)
            cumulative = self.previous_reserved_usd + reserved
            if cumulative > MAX_BUDGET_USD:
                raise ApiBudgetError("cumulative provider API reservation exceeds USD 5.00")
            ledger["reservedUsd"] = round(reserved, 6)
            ledger["cumulativeReservedUsd"] = round(cumulative, 6)
            ledger["resources"][resource_id] = {
                "provider": provider,
                "authMode": AUTH_MODES[provider],
                "ownership": {"taskId": self.task_id, "owner": self.owner, "marker": marker},
                "vendorRef": "",
                "targetRef": target_ref,
                "profileScopeDigestSha256": profile_scope_digest_sha256,
                "targetDigestSha256": target_digest_sha256,
                "pricing": {
                    "currency": "USD",
                    "upperBoundUsd": round(float(upper_bound_usd), 6),
                    "quoteRef": quote_ref,
                    "quotedAt": _iso(quote_time),
                },
                "reservedAt": _iso(now),
                "windowStartedAt": "",
                "windowExpiresAt": "",
                "maxLifetimeSeconds": max_lifetime_seconds,
                "cleanupStartBy": "",
                "cleanupStartedAt": "",
                "windowBreached": False,
                "windowBreachReason": "",
                "desiredState": "DELETED",
                "actualState": "UNKNOWN",
                "readiness": "UNKNOWN",
                "cleanupAbsenceStatus": "PENDING",
                "cleanupReadbackAt": "",
                "cleanupStateDigestSha256": "",
                "writes": [],
            }
            ledger["events"].append(
                {
                    "action": "RESOURCE_RESERVED",
                    "resourceId": resource_id,
                    "reservedUsd": round(float(upper_bound_usd), 6),
                    "recordedAt": _iso(now),
                }
            )

    def begin_mutation(
        self,
        resource_id: str,
        *,
        tool_id: str,
        operation_id: str,
        request_digest_sha256: str,
        resource_ref: str,
        timeout_seconds: int = 300,
    ) -> str:
        """Persist write-ahead evidence before the caller dispatches through DSH."""

        if WRITE_TOOL_IDS.get(operation_id) != tool_id:
            raise ApiBudgetError("tool ID does not match the fixed provider operation map")
        if not SHA256_RE.fullmatch(request_digest_sha256):
            raise ApiBudgetError("request digest must be a lowercase SHA-256 value")
        if (
            not isinstance(resource_ref, str)
            or not resource_ref
            or len(resource_ref) > 4096
            or SENSITIVE_TEXT_RE.search(resource_ref)
        ):
            raise ApiBudgetError("resource reference must be host-derived before dispatch")
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= MAX_RESOURCE_LIFETIME_SECONDS
        ):
            raise ApiBudgetError("provider call timeout must be between 1 and 900 seconds")
        now = _now_utc()
        write_id = str(uuid.uuid4())
        self._verify_predecessor_unchanged()
        with self._locked_ledger() as ledger:
            if ledger["status"] != "IN_PROGRESS":
                raise ApiBudgetError("finalized API evidence cannot be mutated")
            resource = _resource(ledger, resource_id)
            if resource["cleanupAbsenceStatus"] == "ABSENT":
                raise ApiBudgetError("a verified-absent resource cannot receive more writes")
            unresolved = [
                row
                for row in resource["writes"]
                if row["state"] in {"DISPATCH_PENDING", "UNKNOWN", "READBACK_PENDING"}
            ]
            if unresolved:
                raise ApiBudgetError(
                    "reconcile the prior write through typed read-back before dispatch"
                )
            if operation_id == "cloud.resource.restart" and resource["provider"] != "runpod":
                raise ApiBudgetError("Restart is available only for RunPod Pods")
            if operation_id == "cloud.resource.delete" and not any(
                row["operationId"] in {"cloud.resource.create", "cloud.resource.deploy"}
                for row in resource["writes"]
            ):
                raise ApiBudgetError(
                    "test resources must be created through this ledger before deletion"
                )
            cleanup_operation = operation_id in {"cloud.resource.delete", "cloud.operation.cancel"}
            if not cleanup_operation:
                _require_before_cleanup_cutoff(resource, now)
                if resource.get("cleanupStartedAt"):
                    raise ApiBudgetError(
                        "no non-cleanup provider writes are allowed after cleanup starts"
                    )
            elif operation_id == "cloud.resource.delete":
                if not resource.get("cleanupStartedAt"):
                    _require_before_cleanup_cutoff(resource, now)
                    resource["cleanupStartedAt"] = _iso(now)
            elif operation_id == "cloud.operation.cancel" and not resource.get("cleanupStartedAt"):
                _require_before_cleanup_cutoff(resource, now)
                resource["cleanupStartedAt"] = _iso(now)
            if not resource["windowStartedAt"]:
                resource["windowStartedAt"] = _iso(now)
                expires = now + timedelta(seconds=resource["maxLifetimeSeconds"])
                cleanup_start = expires - timedelta(
                    seconds=min(CLEANUP_RESERVE_SECONDS, resource["maxLifetimeSeconds"] // 3)
                )
                resource["windowExpiresAt"] = _iso(expires)
                resource["cleanupStartBy"] = _iso(cleanup_start)
            elif now > _timestamp(
                resource["windowExpiresAt"], "resource expiry"
            ) and operation_id not in {
                "cloud.resource.delete",
                "cloud.operation.cancel",
            }:
                raise ApiBudgetError(
                    "resource lifetime elapsed; only reconciliation and cleanup may continue"
                )
            if not cleanup_operation and now + timedelta(seconds=timeout_seconds) > _timestamp(
                resource["cleanupStartBy"], "cleanup start deadline"
            ):
                raise ApiBudgetError(
                    "provider write timeout would consume the reserved cleanup window"
                )
            if operation_id == "cloud.resource.delete" and not resource.get("cleanupStartedAt"):
                raise ApiBudgetError("cleanup must start before the reserved cleanup deadline")
            if (
                operation_id == "cloud.resource.delete"
                and resource["windowExpiresAt"]
                and now < _timestamp(resource["windowExpiresAt"], "resource expiry")
                and now + timedelta(seconds=timeout_seconds)
                > _timestamp(resource["windowExpiresAt"], "resource expiry")
            ):
                raise ApiBudgetError("delete timeout exceeds the remaining cleanup lifetime")
            write = {
                "writeId": write_id,
                "toolId": tool_id,
                "operationId": operation_id,
                "requestDigestSha256": request_digest_sha256,
                "resourceRef": resource_ref,
                "timeoutSeconds": timeout_seconds,
                "grantRef": "",
                "workOperationId": "",
                "outcome": "PENDING",
                "operationRef": "",
                "state": "DISPATCH_PENDING",
                "startedAt": _iso(now),
                "stateReadback": None,
                "resultDigestSha256": "",
            }
            resource["writes"].append(write)
            ledger["events"].append(
                {
                    "action": "WRITE_AHEAD_RESERVED",
                    "resourceId": resource_id,
                    "writeId": write_id,
                    "operationId": operation_id,
                    "requestDigestSha256": request_digest_sha256,
                    "recordedAt": _iso(now),
                }
            )
        return write_id

    def record_dispatch(
        self,
        resource_id: str,
        write_id: str,
        *,
        grant_ref: str,
        work_operation_id: str,
        outcome: str,
        operation_ref: str = "",
    ) -> None:
        """Record the durable DSH/Exchange/provider operation reference after dispatch."""

        if outcome not in {"ACCEPTED", "UNKNOWN", "REJECTED"}:
            raise ApiBudgetError("dispatch outcome must be ACCEPTED, UNKNOWN, or REJECTED")
        if outcome == "ACCEPTED" and not all(
            isinstance(value, str) and value and len(value) <= 4096
            for value in (grant_ref, work_operation_id, operation_ref)
        ):
            raise ApiBudgetError(
                "accepted dispatch requires grant, Work, and provider operation refs"
            )
        if outcome == "ACCEPTED" and any(
            len(value) > 256 or SENSITIVE_TEXT_RE.search(value)
            for value in (grant_ref, work_operation_id)
        ):
            raise ApiBudgetError("grant and Work references must remain bounded opaque identifiers")
        received_at = _now_utc()
        with self._locked_ledger() as ledger:
            if ledger["status"] != "IN_PROGRESS":
                raise ApiBudgetError("finalized API evidence cannot be mutated")
            resource, write = _write(ledger, resource_id, write_id)
            if write["state"] != "DISPATCH_PENDING":
                raise ApiBudgetError(
                    "dispatch can be recorded only once for a write-ahead reservation"
                )
            start = _timestamp(write["startedAt"], "write-ahead timestamp")
            resource_expiry = _timestamp(resource["windowExpiresAt"], "resource expiry")
            cleanup_cutoff = _timestamp(resource["cleanupStartBy"], "cleanup start deadline")
            late_reply = received_at > start + timedelta(seconds=write["timeoutSeconds"])
            late_write_window = (
                write["operationId"] not in {"cloud.resource.delete", "cloud.operation.cancel"}
                and received_at >= cleanup_cutoff
            )
            late_resource_window = received_at > resource_expiry
            if late_reply or late_write_window or late_resource_window:
                reason = (
                    "dispatch response exceeded its operation timeout"
                    if late_reply
                    else "non-cleanup write crossed the reserved cleanup deadline"
                    if late_write_window
                    else "dispatch response crossed the 900-second resource deadline"
                )
                resource["windowBreached"] = True
                resource["windowBreachReason"] = reason
                ledger["events"].append(
                    {
                        "action": "RESOURCE_WINDOW_BREACHED",
                        "resourceId": resource_id,
                        "writeId": write_id,
                        "reason": reason,
                        "recordedAt": _iso(received_at),
                    }
                )
                outcome = "UNKNOWN"
            write.update(
                {
                    "grantRef": grant_ref,
                    "workOperationId": work_operation_id,
                    "operationRef": operation_ref,
                    "outcome": "UNKNOWN" if outcome == "UNKNOWN" else "PENDING",
                    "state": "UNKNOWN" if outcome == "UNKNOWN" else "READBACK_PENDING",
                    "dispatchedAt": _iso(received_at),
                }
            )
            if outcome == "REJECTED":
                write["outcome"] = "FAILED"
                write["state"] = "READBACK_PENDING"
            ledger["events"].append(
                {
                    "action": "WRITE_DISPATCH_RECORDED",
                    "resourceId": resource_id,
                    "writeId": write_id,
                    "outcome": outcome,
                    "recordedAt": _iso(received_at),
                }
            )

    def record_readback(
        self,
        resource_id: str,
        write_id: str,
        *,
        outcome: str,
        state_digest_sha256: str,
        result_digest_sha256: str,
        observed_at: str,
        vendor_ref: str = "",
        actual_state: str,
        readiness: str,
    ) -> None:
        """Reconcile a completed or uncertain write from an independent typed read."""

        if outcome not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
            raise ApiBudgetError("read-back outcome must be SUCCEEDED, FAILED, or UNKNOWN")
        when = _timestamp(observed_at, "operation read-back")
        if when > _now_utc() + timedelta(minutes=1):
            raise ApiBudgetError("operation read-back timestamp is unexpectedly in the future")
        if outcome in {"SUCCEEDED", "FAILED"} and (
            not SHA256_RE.fullmatch(state_digest_sha256)
            or not SHA256_RE.fullmatch(result_digest_sha256)
            or not actual_state
            or not readiness
        ):
            raise ApiBudgetError("verified operation read-back requires state and result digests")
        with self._locked_ledger() as ledger:
            if ledger["status"] != "IN_PROGRESS":
                raise ApiBudgetError("finalized API evidence cannot be mutated")
            resource, write = _write(ledger, resource_id, write_id)
            if write["state"] not in {"READBACK_PENDING", "UNKNOWN"}:
                raise ApiBudgetError(
                    "typed read-back is required only for an unresolved dispatched write"
                )
            dispatched_at = write.get("dispatchedAt")
            if not isinstance(dispatched_at, str) or when < _timestamp(
                dispatched_at, "dispatch timestamp"
            ):
                raise ApiBudgetError(
                    "operation read-back must be observed after the write dispatch"
                )
            resource_expiry = _timestamp(resource["windowExpiresAt"], "resource expiry")
            cleanup_cutoff = _timestamp(resource["cleanupStartBy"], "cleanup start deadline")
            late_write_window = (
                write["operationId"] not in {"cloud.resource.delete", "cloud.operation.cancel"}
                and when >= cleanup_cutoff
            )
            if when > resource_expiry or late_write_window:
                resource["windowBreached"] = True
                resource["windowBreachReason"] = (
                    "write read-back crossed the 900-second resource deadline"
                    if when > resource_expiry
                    else "non-cleanup write read-back crossed the reserved cleanup deadline"
                )
                ledger["events"].append(
                    {
                        "action": "RESOURCE_WINDOW_BREACHED",
                        "resourceId": resource_id,
                        "writeId": write_id,
                        "reason": resource["windowBreachReason"],
                        "recordedAt": _iso(_now_utc()),
                    }
                )
            if outcome == "UNKNOWN":
                write["state"] = "UNKNOWN"
                write["lastReadbackAt"] = _iso(when)
                ledger["events"].append(
                    {
                        "action": "WRITE_RECONCILIATION_UNKNOWN",
                        "resourceId": resource_id,
                        "writeId": write_id,
                        "recordedAt": _iso(_now_utc()),
                    }
                )
                return
            write["outcome"] = outcome
            write["state"] = "VERIFIED" if outcome == "SUCCEEDED" else "FAILED_RECONCILED"
            write["stateReadback"] = {
                "status": "VERIFIED",
                "observedAt": _iso(when),
                "stateDigestSha256": state_digest_sha256,
            }
            write["resultDigestSha256"] = result_digest_sha256
            resource["actualState"] = actual_state
            resource["readiness"] = readiness
            if vendor_ref:
                if resource["vendorRef"] and resource["vendorRef"] != vendor_ref:
                    raise ApiBudgetError(
                        "provider read-back changed the reserved vendor resource reference"
                    )
                resource["vendorRef"] = vendor_ref
            ledger["events"].append(
                {
                    "action": "WRITE_READBACK_RECONCILED",
                    "resourceId": resource_id,
                    "writeId": write_id,
                    "outcome": outcome,
                    "recordedAt": _iso(_now_utc()),
                }
            )

    def record_resource_state(
        self,
        resource_id: str,
        *,
        vendor_ref: str,
        desired_state: str,
        actual_state: str,
        readiness: str,
        observed_at: str,
    ) -> None:
        """Persist a non-secret typed resource state observed through the provider API."""

        if any(
            not isinstance(value, str) or not value
            for value in (vendor_ref, desired_state, actual_state, readiness)
        ):
            raise ApiBudgetError("resource state fields must be non-empty safe strings")
        if len(vendor_ref) > 4096 or SENSITIVE_TEXT_RE.search(vendor_ref):
            raise ApiBudgetError("vendor resource reference must not contain credential material")
        observed = _timestamp(observed_at, "resource state read-back")
        if observed > _now_utc() + timedelta(minutes=1):
            raise ApiBudgetError("resource state read-back timestamp is unexpectedly in the future")
        with self._locked_ledger() as ledger:
            if ledger["status"] != "IN_PROGRESS":
                raise ApiBudgetError("finalized API evidence cannot be mutated")
            resource = _resource(ledger, resource_id)
            if resource["cleanupAbsenceStatus"] == "ABSENT":
                raise ApiBudgetError("resource is already finalized as absent")
            window_started = _timestamp(resource["windowStartedAt"], "resource window start")
            if observed < window_started:
                raise ApiBudgetError(
                    "resource state read-back predates the reserved resource window"
                )
            expiry = _timestamp(resource["windowExpiresAt"], "resource expiry")
            cutoff = _timestamp(resource["cleanupStartBy"], "cleanup start deadline")
            cleanup_started = resource.get("cleanupStartedAt")
            cleanup_in_time = (
                isinstance(cleanup_started, str)
                and bool(cleanup_started)
                and (_timestamp(cleanup_started, "cleanup start") < cutoff)
            )
            if observed > expiry or (observed >= cutoff and not cleanup_in_time):
                resource["windowBreached"] = True
                resource["windowBreachReason"] = (
                    "resource state read-back crossed the resource deadline"
                    if observed > expiry
                    else "resource state read-back reached the cleanup deadline before cleanup started"
                )
            if resource["vendorRef"] and resource["vendorRef"] != vendor_ref:
                raise ApiBudgetError(
                    "provider resource reference changed from its durable first read-back"
                )
            resource["vendorRef"] = vendor_ref
            resource["desiredState"] = desired_state
            resource["actualState"] = actual_state
            resource["readiness"] = readiness
            ledger["events"].append(
                {
                    "action": "RESOURCE_STATE_READBACK",
                    "resourceId": resource_id,
                    "stateDigestSha256": hashlib.sha256(
                        json.dumps(
                            {
                                "desired": desired_state,
                                "actual": actual_state,
                                "readiness": readiness,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                    "recordedAt": _iso(observed),
                }
            )

    def mark_cleanup_absent(
        self,
        resource_id: str,
        *,
        observed_at: str,
        state_digest_sha256: str,
    ) -> None:
        """Finish only after an independent provider GET/LIST proves the resource absent."""

        observed = _timestamp(observed_at, "cleanup absence read-back")
        if observed > _now_utc() + timedelta(minutes=1):
            raise ApiBudgetError(
                "cleanup absence read-back timestamp is unexpectedly in the future"
            )
        if not SHA256_RE.fullmatch(state_digest_sha256):
            raise ApiBudgetError("cleanup read-back requires a state digest")
        with self._locked_ledger() as ledger:
            if ledger["status"] != "IN_PROGRESS":
                raise ApiBudgetError("finalized API evidence cannot be mutated")
            resource = _resource(ledger, resource_id)
            if not resource["vendorRef"]:
                raise ApiBudgetError(
                    "cleanup read-back requires the exact provider-issued resource reference"
                )
            if any(
                row["state"] in {"DISPATCH_PENDING", "UNKNOWN", "READBACK_PENDING"}
                for row in resource["writes"]
            ):
                raise ApiBudgetError(
                    "reconcile every uncertain write before asserting cleanup absence"
                )
            if not any(row["state"] == "VERIFIED" for row in resource["writes"]):
                raise ApiBudgetError(
                    "cleanup evidence requires at least one successful typed write"
                )
            if not any(
                row["operationId"] == "cloud.resource.delete"
                and row["outcome"] == "SUCCEEDED"
                and row["state"] == "VERIFIED"
                for row in resource["writes"]
            ):
                raise ApiBudgetError(
                    "cleanup evidence requires a successful explicit delete operation"
                )
            if not resource.get("cleanupStartedAt"):
                raise ApiBudgetError("cleanup must start inside the reserved resource lifetime")
            deletion = next(
                row
                for row in reversed(resource["writes"])
                if row["operationId"] == "cloud.resource.delete"
                and row["outcome"] == "SUCCEEDED"
                and row["state"] == "VERIFIED"
            )
            delete_observed = _timestamp(
                deletion["stateReadback"]["observedAt"], "delete operation read-back"
            )
            cleanup_started = _timestamp(resource["cleanupStartedAt"], "cleanup start")
            if observed < delete_observed or observed < cleanup_started:
                raise ApiBudgetError(
                    "cleanup absence read-back must follow cleanup dispatch and delete read-back"
                )
            if observed > _timestamp(resource["windowExpiresAt"], "resource expiry"):
                resource["windowBreached"] = True
                resource["windowBreachReason"] = (
                    "cleanup absence read-back crossed the 900-second resource deadline"
                )
            resource["desiredState"] = "DELETED"
            resource["actualState"] = "ABSENT"
            resource["cleanupAbsenceStatus"] = "ABSENT"
            resource["cleanupReadbackAt"] = _iso(observed)
            resource["cleanupStateDigestSha256"] = state_digest_sha256
            ledger["events"].append(
                {
                    "action": "CLEANUP_ABSENCE_VERIFIED",
                    "resourceId": resource_id,
                    "stateDigestSha256": state_digest_sha256,
                    "recordedAt": _iso(_now_utc()),
                }
            )

    def finalize(self, receipt_path: Path) -> dict[str, Any]:
        """Write a REAL_PROVIDER_API receipt only after every owned resource is absent."""

        _private_file(receipt_path, label="API evidence receipt", must_exist=False)
        with self._locked_ledger() as ledger:
            if ledger["status"] == "VERIFIED":
                raise ApiBudgetError("this API ledger has already been finalized")
            expected_providers = PROVIDER_BY_STAGE[self.stage]
            observed_providers = {row["provider"] for row in ledger["resources"].values()}
            if observed_providers != expected_providers:
                raise ApiBudgetError(
                    f"stage requires resources for exactly {sorted(expected_providers)}"
                )
            receipt_providers: list[dict[str, Any]] = []
            for provider in sorted(expected_providers):
                resources = [
                    row for row in ledger["resources"].values() if row["provider"] == provider
                ]
                provider_writes = [write for resource in resources for write in resource["writes"]]
                if (
                    not resources
                    or any(
                        row["cleanupAbsenceStatus"] != "ABSENT"
                        or row["windowBreached"]
                        or not row["vendorRef"]
                        or not row["cleanupReadbackAt"]
                        or not row["cleanupStateDigestSha256"]
                        for row in resources
                    )
                    or not provider_writes
                    or any(
                        write["state"] != "VERIFIED"
                        or write["outcome"] != "SUCCEEDED"
                        or not write["grantRef"]
                        or not write["workOperationId"]
                        or not write["operationRef"]
                        or not write["stateReadback"]
                        or not write["resultDigestSha256"]
                        for write in provider_writes
                    )
                ):
                    raise ApiBudgetError(
                        f"{provider} still lacks successful API read-back or cleanup absence proof"
                    )
                first = resources[0]
                receipt_providers.append(
                    {
                        "providerId": provider,
                        "identityStatus": "VERIFIED",
                        "authMode": first["authMode"],
                        "profileScopeDigestSha256": first["profileScopeDigestSha256"],
                        "resources": [
                            {
                                "ownership": row["ownership"],
                                "vendorRef": row["vendorRef"],
                                "desiredState": row["desiredState"],
                                "actualState": row["actualState"],
                                "readiness": row["readiness"],
                                "cleanupAbsenceStatus": row["cleanupAbsenceStatus"],
                                "cleanupReadbackAt": row["cleanupReadbackAt"],
                            }
                            for row in resources
                        ],
                        "writeOperations": [
                            {
                                "toolId": write["toolId"],
                                "operationId": write["operationId"],
                                "requestDigestSha256": write["requestDigestSha256"],
                                "resourceRef": write["resourceRef"],
                                "grantRef": write["grantRef"],
                                "workOperationId": write["workOperationId"],
                                "outcome": "SUCCEEDED",
                                "operationRef": write["operationRef"],
                                "stateReadback": write["stateReadback"],
                                "resultDigestSha256": write["resultDigestSha256"],
                            }
                            for write in provider_writes
                        ],
                    }
                )
            verified_at = max(
                _timestamp(row["cleanupReadbackAt"], "cleanup read-back")
                for row in ledger["resources"].values()
            )
            receipt = {
                "schemaVersion": "cyrene-cloud-api-evidence-v1",
                "taskId": self.task_id,
                "owner": self.owner,
                "stage": self.stage,
                "status": "VERIFIED",
                "evidenceClass": "REAL_PROVIDER_API",
                "executionPath": "navigator_dsh_exchange_directplugin_api",
                "verifiedAt": _iso(verified_at),
                "reservedUsd": round(float(ledger["reservedUsd"]), 6),
                "cumulativeReservedUsd": round(float(ledger["cumulativeReservedUsd"]), 6),
                "previousEvidenceSha256": self.previous_evidence_sha256,
                "providers": receipt_providers,
            }
            payload = json.dumps(receipt, sort_keys=True, indent=2).encode() + b"\n"
            _write_new_or_identical(receipt_path, payload)
            ledger["status"] = "VERIFIED"
            ledger["receiptSha256"] = hashlib.sha256(payload).hexdigest()
            ledger["verifiedAt"] = receipt["verifiedAt"]
            ledger["events"].append(
                {"action": "API_EVIDENCE_FINALIZED", "recordedAt": receipt["verifiedAt"]}
            )
        return receipt

    def snapshot(self) -> dict[str, Any]:
        """Return safe metadata for a private coordinator without exposing raw inputs."""

        with self._locked_ledger() as ledger:
            return {
                "taskId": ledger["taskId"],
                "owner": ledger["owner"],
                "stage": ledger["stage"],
                "status": ledger["status"],
                "reservedUsd": ledger["reservedUsd"],
                "cumulativeReservedUsd": ledger["cumulativeReservedUsd"],
                "resourceStates": {
                    resource_id: {
                        "provider": row["provider"],
                        "actualState": row["actualState"],
                        "cleanupAbsenceStatus": row["cleanupAbsenceStatus"],
                        "windowBreached": row["windowBreached"],
                        "cleanupStartBy": row["cleanupStartBy"],
                        "windowExpiresAt": row["windowExpiresAt"],
                        "unresolvedWrites": sum(
                            write["state"] in {"DISPATCH_PENDING", "UNKNOWN", "READBACK_PENDING"}
                            for write in row["writes"]
                        ),
                    }
                    for resource_id, row in ledger["resources"].items()
                },
            }

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, Any]) -> None:
        descriptor, filename = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(filename)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
                stream.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _resource(ledger: dict[str, Any], resource_id: str) -> dict[str, Any]:
    row = ledger["resources"].get(resource_id)
    if not isinstance(row, dict):
        raise ApiBudgetError(
            "resource has no durable reservation; do not dispatch a provider write"
        )
    return row


def _write(
    ledger: dict[str, Any], resource_id: str, write_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    resource = _resource(ledger, resource_id)
    matches = [row for row in resource["writes"] if row.get("writeId") == write_id]
    if len(matches) != 1:
        raise ApiBudgetError("write ID is not present in the durable reservation ledger")
    return resource, matches[0]


def _require_before_cleanup_cutoff(resource: dict[str, Any], now: datetime) -> None:
    cutoff = resource.get("cleanupStartBy")
    if cutoff and now >= _timestamp(cutoff, "cleanup start deadline"):
        raise ApiBudgetError(
            "cleanup reserve has started; stop acceptance writes and begin cleanup"
        )


def _check_reservation_totals(ledger: dict[str, Any]) -> None:
    rows = ledger.get("resources", {}).values()
    reserved = sum(float(row["pricing"]["upperBoundUsd"]) for row in rows)
    cumulative = float(ledger.get("previousReservedUsd", 0)) + reserved
    if (
        reserved > MAX_BUDGET_USD
        or cumulative > MAX_BUDGET_USD
        or not math.isclose(reserved, float(ledger.get("reservedUsd", -1)), abs_tol=0.000001)
        or not math.isclose(
            cumulative, float(ledger.get("cumulativeReservedUsd", -1)), abs_tol=0.000001
        )
    ):
        raise ApiBudgetError("API reservation ledger spend totals are inconsistent or exceed USD 5")


def _write_new_or_identical(path: Path, payload: bytes) -> None:
    """Create a private final receipt without replacing an existing result."""

    if path.exists():
        _existing, raw = _read_json(path, "API evidence receipt")
        if raw != payload:
            raise ApiBudgetError(
                "a different API evidence receipt already exists; reconcile manually"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_uid != os.geteuid() or path.parent.stat().st_mode & 0o077:
        raise ApiBudgetError("API evidence directory must be owner-only")
    ApiAcceptanceLedger._atomic_write(path, json.loads(payload))
