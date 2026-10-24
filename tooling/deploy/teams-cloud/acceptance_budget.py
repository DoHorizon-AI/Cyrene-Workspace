"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 acceptance_budget.py                                                 │
│  Module: tooling.deploy.teams_cloud.acceptance_budget                   │
│  Role: Run bounded cloud acceptance with ownership, pricing, and cleanup. │
│                                                                          │
│  模块职责：按资源权属、报价和预算执行有界验收并回收资源。                │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import resource as resource_limits
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MAX_BUDGET_USD = 5.0
MAX_ACTION_SECONDS = 900
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_PRICE_AGE = timedelta(hours=24)
APPROVED_EXECUTABLES = {"az", "gcloud", "runpodctl"}
FORBIDDEN_ARG_RE = re.compile(
    r"(password|passwd|token|secret|private.?key|client.?secret|api.?key|access.?key|credential|authorization)",
    re.IGNORECASE,
)
TASK_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,43}$")
PLACEHOLDER_RE = re.compile(
    r"(replace[_ -]?with|example\.invalid|changeme|placeholder|<[^>]+>)", re.IGNORECASE
)
_AZURE_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_CLI_CONFIG_ROOT: Path | None = None
_AZURE_CLI_READY = False


class BudgetError(ValueError):
    """The acceptance plan or live resource state cannot be proven safe."""


def _parse_time(value: Any, name: str) -> datetime:
    """Parse one timezone-aware ISO-8601 timestamp."""

    if not isinstance(value, str) or not value:
        raise BudgetError(f"{name} requires a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise BudgetError(f"{name} is not a valid ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise BudgetError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _is_private_artifact(path: Path) -> bool:
    """Require plan and ledger material to live outside any source checkout."""

    resolved = path.resolve()
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            return False
    return ".artifacts" in resolved.parts


def _validate_argv(
    action: Any,
    name: str,
    marker: str | None = None,
    expected_executable: str | None = None,
) -> list[str]:
    """Validate one vendor CLI call as a bounded argv array, never a shell string."""

    if not isinstance(action, dict):
        raise BudgetError(f"{name} must be an argv object")
    argv = action.get("argv")
    timeout = action.get("timeoutSeconds")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(part, str) or not part for part in argv)
    ):
        raise BudgetError(f"{name}.argv must be a non-empty string array")
    if argv[0] not in APPROVED_EXECUTABLES:
        raise BudgetError(f"{name} executable must be one of the pinned cloud CLIs")
    if expected_executable is not None and argv[0] != expected_executable:
        raise BudgetError(f"{name} executable does not match its bound provider")
    if not shutil.which(argv[0]):
        raise BudgetError(f"required executable is unavailable: {argv[0]}")
    if any(part in {"sh", "bash", "-c", "--command"} for part in argv):
        raise BudgetError(f"{name} must not invoke a shell")
    if any(FORBIDDEN_ARG_RE.search(part) for part in argv):
        raise BudgetError(f"{name} must not put credential values in argv")
    if marker is not None and marker not in "\x00".join(argv):
        raise BudgetError(f"{name} must bind the exact owner marker in its argv")
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= MAX_ACTION_SECONDS
    ):
        raise BudgetError(f"{name}.timeoutSeconds must be between 1 and 900")
    return argv


def _validate_readback_argv(
    action: Any, name: str, marker: str, expected_executable: str
) -> list[str]:
    """Allow unfiltered list commands only when local exact-marker matching is configured."""

    marker_in_argv = action.get("markerInArgv", True) if isinstance(action, dict) else None
    if not isinstance(marker_in_argv, bool):
        raise BudgetError(f"{name}.markerInArgv must be a boolean")
    return _validate_argv(
        action,
        name,
        marker if marker_in_argv else None,
        expected_executable,
    )


def _validate_provider_action(
    action: Any,
    name: str,
    provider: str,
    *,
    marker: str | None = None,
    is_readback: bool = False,
) -> list[str]:
    """Validate executable identity and keep its scope within one bound profile."""

    expected = {"azure": "az", "gcp": "gcloud", "runpod": "runpodctl"}[provider]
    if is_readback:
        if marker is None:
            raise BudgetError(f"{name} must define its exact owner marker")
        argv = _validate_readback_argv(action, name, marker, expected)
    else:
        argv = _validate_argv(action, name, marker, expected)
    _validate_cli_scope(provider, argv, marker)
    return argv


def _read_previous_evidence(
    path: Path, *, expected_stage: str, task_id: str, owner: str
) -> tuple[dict[str, Any], float]:
    """Require the exact prior acceptance stage and carry its reservation forward."""

    if (
        not _is_private_artifact(path)
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != os.geteuid()
        or path.stat().st_mode & 0o077
    ):
        raise BudgetError("the previous phase evidence must be stored under umbrella .artifacts")
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BudgetError("previous phase evidence is not readable JSON") from error
    expected_schema = (
        "cyrene-cloud-acceptance-evidence-v1"
        if expected_stage == "github_hf"
        else "cyrene-cloud-acceptance-ledger-v1"
    )
    if (
        not isinstance(evidence, dict)
        or evidence.get("schemaVersion") != expected_schema
        or evidence.get("stage") != expected_stage
        or evidence.get("status") != "VERIFIED"
        or evidence.get("taskId") != task_id
        or evidence.get("owner") != owner
        or PLACEHOLDER_RE.search(json.dumps(evidence, sort_keys=True))
    ):
        raise BudgetError(
            f"stage evidence must be VERIFIED for {expected_stage} and the same task ID"
        )
    if expected_stage == "github_hf":
        providers = evidence.get("providers")
        if (
            evidence.get("evidenceClass") != "REAL_PROVIDER_API"
            or evidence.get("reservedUsd") != 0
            or evidence.get("cumulativeReservedUsd") != 0
            or not isinstance(evidence.get("verifiedAt"), str)
            or not isinstance(providers, dict)
            or set(providers) != {"github", "huggingface"}
        ):
            raise BudgetError("GitHub/HF evidence must contain a real, zero-cost provider receipt")
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
                or not isinstance(result.get("cleanupReadbackAt"), str)
            ):
                raise BudgetError(
                    f"{provider} must have a private read/write/read-back and clean receipt"
                )
    else:
        resources = evidence.get("resources")
        if (
            evidence.get("evidenceClass") != "REAL_VENDOR_CLI"
            or not isinstance(evidence.get("previousEvidenceSha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", evidence["previousEvidenceSha256"])
            or not isinstance(evidence.get("cleanupVerifiedAt"), str)
            or not isinstance(resources, dict)
            or not resources
        ):
            raise BudgetError("Azure/GCP evidence must prove a real, cleaned provider ledger")
        observed_providers: set[str] = set()
        for resource in resources.values():
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
                not isinstance(resource, dict)
                or resource.get("state") != "DELETED"
                or not absence_verified
            ):
                raise BudgetError(
                    "Azure/GCP ledger contains a resource without verified absence read-back"
                )
            ownership = resource.get("ownership")
            if (
                not isinstance(ownership, dict)
                or ownership.get("taskId") != task_id
                or ownership.get("owner") != owner
                or not isinstance(ownership.get("marker"), str)
                or task_id not in ownership["marker"]
                or not isinstance(resource.get("provider"), str)
            ):
                raise BudgetError("Azure/GCP ledger resource ownership does not match this task")
            observed_providers.add(resource.get("provider"))
        if observed_providers != {"azure", "gcp"}:
            raise BudgetError("Azure/GCP ledger must include exactly both providers")
    reserved = evidence.get("cumulativeReservedUsd", evidence.get("reservedUsd"))
    if (
        not isinstance(reserved, (int, float))
        or isinstance(reserved, bool)
        or not math.isfinite(reserved)
        or reserved < 0
        or reserved > MAX_BUDGET_USD
    ):
        raise BudgetError("previous phase evidence has no valid reserved USD total")
    if expected_stage == "github_hf" and reserved != 0:
        raise BudgetError("GitHub/Hugging Face acceptance must not reserve cloud-resource spend")
    return evidence, float(reserved)


def _json_pointer(value: Any, pointer: Any) -> Any:
    """Read a JSON Pointer path from one vendor's structured response."""

    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise BudgetError("response pointers must be absolute JSON Pointers")
    selected = value
    for item in pointer[1:].split("/") if pointer else []:
        part = item.replace("~1", "/").replace("~0", "~")
        if isinstance(selected, list):
            try:
                selected = selected[int(part)]
            except (ValueError, IndexError) as error:
                raise BudgetError("response pointer did not match provider JSON") from error
        elif isinstance(selected, dict) and part in selected:
            selected = selected[part]
        else:
            raise BudgetError("response pointer did not match provider JSON")
    return selected


def validate_plan(
    path: Path,
    *,
    now: datetime | None = None,
    previous_evidence: Path | None = None,
    for_cleanup: bool = False,
) -> dict[str, Any]:
    """Validate task ordering, fresh price bounds, owner markers, and CLI actions."""

    if (
        not _is_private_artifact(path)
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != os.geteuid()
        or path.stat().st_mode & 0o077
    ):
        raise BudgetError("acceptance plans must be stored under the umbrella .artifacts directory")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BudgetError("acceptance plan is not readable JSON") from error
    if not isinstance(plan, dict) or plan.get("schemaVersion") != "cyrene-cloud-acceptance-plan-v1":
        raise BudgetError("unsupported cloud acceptance plan schema")
    if PLACEHOLDER_RE.search(json.dumps(plan, sort_keys=True)):
        raise BudgetError("acceptance plan still contains a placeholder")
    task_id = plan.get("taskId")
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise BudgetError("plan taskId must be explicit and non-placeholder")
    if task_id == "teams-cloud-20261023":
        raise BudgetError(
            "this deployment task reserves billable lifecycle writes for the formal DSH API path; vendor-CLI mutation plans are disabled"
        )
    if plan.get("budgetUsd") != MAX_BUDGET_USD:
        raise BudgetError("acceptance task budget is fixed at USD 5.00")
    owner = plan.get("owner")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 256:
        raise BudgetError("plan owner must name the authorized maintenance identity")
    stage = plan.get("stage")
    if stage not in {"azure_gcp", "runpod"}:
        raise BudgetError("billable resource stages must be azure_gcp or runpod")
    if not for_cleanup:
        if previous_evidence is None or not _is_private_artifact(previous_evidence):
            raise BudgetError("the previous phase evidence must be provided from .artifacts")
        expected_previous = "github_hf" if stage == "azure_gcp" else "azure_gcp"
        _, previous_reserved = _read_previous_evidence(
            previous_evidence,
            expected_stage=expected_previous,
            task_id=task_id,
            owner=owner,
        )
    else:
        previous_reserved = 0.0

    resources = plan.get("resources")
    if not isinstance(resources, list) or not resources:
        raise BudgetError("a billable acceptance plan must list at least one owned resource")
    allowed_provider = {"azure", "gcp"} if stage == "azure_gcp" else {"runpod"}
    ids: set[str] = set()
    total = 0.0
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    for resource in resources:
        if not isinstance(resource, dict):
            raise BudgetError("every resource reservation must be an object")
        resource_id, provider = resource.get("id"), resource.get("provider")
        if not isinstance(resource_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{2,80}", resource_id
        ):
            raise BudgetError("resource ID must be a stable non-placeholder identifier")
        if resource_id in ids:
            raise BudgetError("resource IDs must be unique in one acceptance plan")
        ids.add(resource_id)
        if provider not in allowed_provider:
            raise BudgetError(f"provider {provider!r} is not allowed in stage {stage}")
        lifetime = resource.get("maxLifetimeSeconds")
        if (
            not isinstance(lifetime, int)
            or isinstance(lifetime, bool)
            or not 1 <= lifetime <= MAX_ACTION_SECONDS
        ):
            raise BudgetError("each billable resource lifetime must not exceed 15 minutes")
        ownership = resource.get("ownership")
        if (
            not isinstance(ownership, dict)
            or ownership.get("taskId") != task_id
            or ownership.get("owner") != plan.get("owner")
        ):
            raise BudgetError(f"resource {resource_id} must include this task ID and named owner")
        marker = ownership.get("marker")
        if not isinstance(marker, str) or task_id not in marker or len(marker) > 180:
            raise BudgetError(f"resource {resource_id} must use a task-specific ownership marker")
        pricing = resource.get("pricing")
        if not isinstance(pricing, dict) or pricing.get("currency") != "USD":
            raise BudgetError(f"resource {resource_id} has no USD price bound")
        amount = pricing.get("upperBoundUsd")
        if (
            not isinstance(amount, (int, float))
            or isinstance(amount, bool)
            or not math.isfinite(amount)
            or amount < 0
        ):
            raise BudgetError(f"resource {resource_id} has no finite price upper bound")
        quote_ref = pricing.get("quoteRef")
        parsed_quote = urlparse(quote_ref) if isinstance(quote_ref, str) else None
        if parsed_quote is None or parsed_quote.scheme != "https" or not parsed_quote.hostname:
            raise BudgetError(f"resource {resource_id} needs a direct price quote reference")
        quote_time = _parse_time(pricing.get("quotedAt"), f"resource {resource_id} quotedAt")
        if not for_cleanup and (quote_time > clock or clock - quote_time > MAX_PRICE_AGE):
            raise BudgetError(
                f"resource {resource_id} price quote is future-dated or older than 24 hours"
            )
        total += float(amount)
        create = resource.get("create")
        _validate_provider_action(create, f"resource {resource_id} create", provider, marker=marker)
        readback = resource.get("readback")
        _validate_provider_action(
            readback,
            f"resource {resource_id} readback",
            provider,
            marker=marker,
            is_readback=True,
        )
        if not isinstance(readback.get("itemsPointer"), str) or any(
            not isinstance(readback.get(key), str) or not readback[key].startswith("/")
            for key in ("markerPointer", "referencePointer")
        ):
            raise BudgetError(f"resource {resource_id} readback JSON pointers are required")
        if readback.get("markerValue") != marker:
            raise BudgetError(
                f"resource {resource_id} create read-back must match its owner marker"
            )
        cleanup = resource.get("cleanup")
        if not isinstance(cleanup, dict):
            raise BudgetError(f"resource {resource_id} cleanup action is required")
        _validate_provider_action(cleanup, f"resource {resource_id} cleanup", provider)
        cleanup_argv = cleanup.get("argv")
        if not any(
            token in " ".join(cleanup_argv).lower() for token in ("delete", "remove", "terminate")
        ):
            raise BudgetError(
                f"resource {resource_id} cleanup must explicitly delete the exact resource"
            )
        if "{resource_ref}" not in "\x00".join(cleanup_argv):
            raise BudgetError(
                f"resource {resource_id} cleanup must use the exact read-back resource reference"
            )
        verify = resource.get("cleanupReadback")
        _validate_provider_action(
            verify,
            f"resource {resource_id} cleanupReadback",
            provider,
            marker=marker,
            is_readback=True,
        )
        if (
            not isinstance(verify.get("itemsPointer"), str)
            or not isinstance(verify.get("markerPointer"), str)
            or not verify["markerPointer"].startswith("/")
        ):
            raise BudgetError(f"resource {resource_id} cleanup read-back pointers are required")
        if verify.get("markerValue") != marker:
            raise BudgetError(
                f"resource {resource_id} cleanup read-back must match its owner marker"
            )
        exercise = resource.get("exercise")
        if not isinstance(exercise, list) or not exercise:
            raise BudgetError(
                f"resource {resource_id} needs at least one read-back-verified acceptance operation"
            )
        time_reservation = sum(
            action["timeoutSeconds"] for action in (create, readback, cleanup, verify)
        )
        for index, step in enumerate(exercise):
            if not isinstance(step, dict) or not re.fullmatch(
                r"[a-zA-Z0-9._-]{1,80}", str(step.get("name", ""))
            ):
                raise BudgetError(f"resource {resource_id} exercise {index} requires a stable name")
            operation = step.get("action")
            _validate_provider_action(
                operation, f"resource {resource_id} exercise action", provider
            )
            operation_argv = operation["argv"]
            if "{resource_ref}" not in "\x00".join(operation_argv):
                raise BudgetError(
                    f"resource {resource_id} exercise action must use the exact resource reference"
                )
            result_readback = step.get("readback")
            _validate_provider_action(
                result_readback,
                f"resource {resource_id} exercise readback",
                provider,
                marker=marker,
                is_readback=True,
            )
            if not isinstance(result_readback.get("itemsPointer"), str) or not all(
                isinstance(result_readback.get(key), str) and result_readback[key].startswith("/")
                for key in ("markerPointer", "referencePointer", "expectedPointer")
            ):
                raise BudgetError(f"resource {resource_id} exercise readback pointers are required")
            if result_readback.get("markerValue") != marker:
                raise BudgetError(
                    f"resource {resource_id} exercise read-back must match its owner marker"
                )
            expected_value = result_readback.get("expectedValue")
            if not isinstance(expected_value, (str, int, float, bool)) or (
                isinstance(expected_value, float) and not math.isfinite(expected_value)
            ):
                raise BudgetError(
                    f"resource {resource_id} exercise needs an exact expected read-back value"
                )
            time_reservation += operation["timeoutSeconds"] + result_readback["timeoutSeconds"]
        if time_reservation + 15 > lifetime:
            raise BudgetError(
                f"resource {resource_id} worst-case action time exceeds its {lifetime}-second lifetime"
            )
    providers = {row.get("provider") for row in resources if isinstance(row, dict)}
    if providers != allowed_provider:
        raise BudgetError(f"stage {stage} requires exactly {sorted(allowed_provider)} resources")
    if not for_cleanup and total + previous_reserved > MAX_BUDGET_USD:
        raise BudgetError(
            f"cumulative reserved upper bound ${total + previous_reserved:.2f} exceeds the $5.00 task budget"
        )
    return plan


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    """Persist ledger transitions before every potentially billable operation."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
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


def _load_ledger(
    path: Path,
    plan: dict[str, Any],
    *,
    previous_reserved: float = 0.0,
    previous_evidence_sha256: str | None = None,
) -> dict[str, Any]:
    """Load or initialize a task ledger with the immutable reservation ceiling."""

    if path.is_symlink():
        raise BudgetError("acceptance ledger must not be a symlink")
    if path.exists():
        metadata = path.stat()
        if metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid():
            raise BudgetError("acceptance ledger permissions, owner, or file type are unsafe")
        try:
            ledger = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BudgetError("acceptance ledger is unreadable") from error
        if (
            ledger.get("taskId") != plan["taskId"]
            or ledger.get("owner") != plan["owner"]
            or ledger.get("stage") != plan["stage"]
            or ledger.get("planSha256")
            != hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        ):
            raise BudgetError("ledger belongs to a different task or plan; reconcile manually")
        if set(ledger.get("resources", {})) != {row["id"] for row in plan["resources"]}:
            raise BudgetError("ledger resource references do not match the immutable plan")
        if previous_evidence_sha256 is not None and (
            ledger.get("previousEvidenceSha256") != previous_evidence_sha256
            or ledger.get("previousReservedUsd") != previous_reserved
        ):
            raise BudgetError("ledger is bound to different previous-stage evidence")
        return ledger
    return {
        "schemaVersion": "cyrene-cloud-acceptance-ledger-v1",
        "evidenceClass": "REAL_VENDOR_CLI",
        "stage": plan["stage"],
        "status": "PLANNED",
        "taskId": plan["taskId"],
        "owner": plan["owner"],
        "planSha256": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
        "budgetUsd": MAX_BUDGET_USD,
        "reservedUsd": sum(float(row["pricing"]["upperBoundUsd"]) for row in plan["resources"]),
        "previousReservedUsd": previous_reserved,
        "previousEvidenceSha256": previous_evidence_sha256,
        "cumulativeReservedUsd": previous_reserved
        + sum(float(row["pricing"]["upperBoundUsd"]) for row in plan["resources"]),
        "resources": {
            row["id"]: {
                "provider": row["provider"],
                "ownership": row["ownership"],
                "vendorResourceId": row["id"],
                "pricing": row["pricing"],
                "maxLifetimeSeconds": row["maxLifetimeSeconds"],
                "state": "PLANNED",
                "events": [],
            }
            for row in plan["resources"]
        },
    }


def _cloud_profiles() -> list[dict[str, Any]]:
    """Read the provider scopes from the host-owned, non-secret activation JSON."""

    try:
        config = json.loads(os.environ.get("CYRENE_CAPABILITY_CONFIGURATION_JSON", "{}"))
    except json.JSONDecodeError as error:
        raise BudgetError("cloud provider profile configuration is invalid") from error
    profiles = config.get("profiles") if isinstance(config, dict) else None
    if not isinstance(profiles, list):
        raise BudgetError("cloud provider profile configuration is unavailable")
    return [row for row in profiles if isinstance(row, dict)]


def _cloud_profile(provider: str) -> dict[str, Any]:
    """Require one unambiguous host profile for a bounded acceptance stage."""

    matches = [row for row in _cloud_profiles() if row.get("provider") == provider]
    if len(matches) != 1:
        raise BudgetError(f"acceptance requires exactly one {provider} cloud profile")
    return matches[0]


def _flag_values(argv: list[str], names: set[str]) -> list[str]:
    """Return values supplied to exact CLI flags without interpreting a shell string."""

    result: list[str] = []
    index = 0
    while index < len(argv):
        part = argv[index]
        if part in names and index + 1 < len(argv):
            result.append(argv[index + 1])
            index += 2
            continue
        for name in names:
            if part.startswith(f"{name}="):
                result.append(part.split("=", 1)[1])
                break
        index += 1
    return result


def _validate_cli_scope(provider: str, argv: list[str], ownership_marker: str | None) -> None:
    """Keep direct acceptance CLIs within the configured cloud profile scope."""

    profile = _cloud_profile(provider)
    scope = profile.get("scope")
    if not isinstance(scope, dict):
        raise BudgetError(f"{provider} cloud profile scope is invalid")
    if provider == "azure":
        subscription = scope.get("subscription_id")
        groups = scope.get("resource_groups")
        if not isinstance(subscription, str) or not isinstance(groups, list) or not groups:
            raise BudgetError("Azure subscription and resource-group scope are required")
        if any(
            value.casefold() != subscription.casefold()
            for value in _flag_values(argv, {"--subscription", "-s"})
        ):
            raise BudgetError("Azure CLI action exceeds the bound subscription")
        group_values = _flag_values(argv, {"--resource-group", "-g"})
        if any(
            value.casefold() not in {str(group).casefold() for group in groups}
            for value in group_values
        ):
            raise BudgetError("Azure CLI action exceeds the bound resource groups")
        resource_ids = _flag_values(argv, {"--ids"})
        for resource_id in resource_ids:
            if resource_id == "{resource_ref}":
                continue
            _validate_azure_resource_id(resource_id, subscription, groups)
        if "resource" in argv and "create" in argv:
            tags = _flag_values(argv, {"--tags"})
            if (
                not isinstance(ownership_marker, str)
                or f"cyreneTask={ownership_marker}" not in tags
            ):
                raise BudgetError("Azure resource create must apply the exact task ownership tag")
        if not group_values and not resource_ids:
            raise BudgetError(
                "Azure CLI action must name a bound resource group or exact resource ID"
            )
        return
    if provider == "gcp":
        project = scope.get("project_id")
        zones = scope.get("zones")
        prefixes = scope.get("instance_name_prefixes")
        if (
            not isinstance(project, str)
            or not isinstance(zones, list)
            or not isinstance(prefixes, list)
        ):
            raise BudgetError("GCP project, zone, and resource-name scopes are required")
        project_values = _flag_values(argv, {"--project", "-p"})
        if not project_values or any(value != project for value in project_values):
            raise BudgetError("GCP CLI action exceeds the bound project")
        if any(value not in zones for value in _flag_values(argv, {"--zone", "-z"})):
            raise BudgetError("GCP CLI action exceeds the bound zones")
        if any(value not in zones for value in _flag_values(argv, {"--zones"})):
            raise BudgetError("GCP CLI action exceeds the bound zones")
        instance_commands = [
            (index, argv[index + 2])
            for index in range(len(argv) - 2)
            if argv[index : index + 2] == ["compute", "instances"]
        ]
        if instance_commands:
            _, instance_action = instance_commands[0]
            zones = _flag_values(argv, {"--zone", "-z"})
            if instance_action in {"create", "describe", "delete", "start", "stop", "reset"} and (
                len(zones) != 1 or zones[0] not in scope["zones"]
            ):
                raise BudgetError("GCP instance lifecycle actions require one bound zone")
        for index in range(len(argv) - 2):
            if argv[index : index + 3] == ["compute", "instances", "create"]:
                name = argv[index + 3] if len(argv) > index + 3 else ""
                labels = _flag_values(argv, {"--labels"})
                if (
                    not isinstance(ownership_marker, str)
                    or name != ownership_marker
                    or not re.fullmatch(r"[a-z][-a-z0-9]{0,61}", name)
                    or not any(
                        isinstance(prefix, str) and name.startswith(prefix) for prefix in prefixes
                    )
                    or f"cyrene-task={ownership_marker}" not in labels
                ):
                    raise BudgetError(
                        "GCP instance create must use the exact owner name, label, and prefix scope"
                    )
                break
        return
    if provider == "runpod":
        if len(argv) < 3 or argv[1].casefold() != "pod":
            raise BudgetError("RunPod acceptance is limited to Pod commands")
        action = argv[2].casefold()
        if action not in {
            "create",
            "list",
            "get",
            "start",
            "stop",
            "restart",
            "update",
            "delete",
            "logs",
        }:
            raise BudgetError("RunPod acceptance command is outside the Pod lifecycle")
        if action == "create":
            name_values = _flag_values(argv, {"--name"})
            prefixes = scope.get("pod_name_prefixes")
            if (
                len(name_values) != 1
                or not isinstance(ownership_marker, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", name_values[0])
                or not isinstance(prefixes, list)
                or not any(
                    isinstance(prefix, str) and name_values[0].startswith(prefix)
                    for prefix in prefixes
                )
                or name_values[0] != ownership_marker
            ):
                raise BudgetError(
                    "RunPod Pod name must equal the task marker and match its allowed prefix"
                )
        return
    raise BudgetError("unsupported provider scope")


def _validate_azure_resource_id(resource_id: str, subscription: str, groups: list[Any]) -> None:
    """Bind a returned Azure resource ID to the exact subscription and group."""

    match = re.fullmatch(
        r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/.+",
        resource_id,
        re.IGNORECASE,
    )
    if (
        not match
        or match.group(1).casefold() != subscription.casefold()
        or match.group(2).casefold() not in {str(group).casefold() for group in groups}
    ):
        raise BudgetError("Azure resource reference is outside the bound subscription or groups")


def _validate_resource_reference(resource: dict[str, Any], reference: Any) -> str:
    """Reject provider references that escaped the task's configured scope."""

    if not isinstance(reference, str) or not reference or len(reference) > 2048:
        raise BudgetError(f"{resource['id']} has an invalid provider resource reference")
    provider = resource["provider"]
    profile = _cloud_profile(provider)
    scope = profile.get("scope")
    if not isinstance(scope, dict):
        raise BudgetError(f"{provider} cloud profile scope is invalid")
    if provider == "azure":
        _validate_azure_resource_id(
            reference, str(scope["subscription_id"]), scope["resource_groups"]
        )
    elif provider == "gcp":
        prefixes = scope.get("instance_name_prefixes")
        if (
            not re.fullmatch(r"[a-z][-a-z0-9]{0,61}", reference)
            or not isinstance(prefixes, list)
            or not any(
                reference.startswith(prefix) for prefix in prefixes if isinstance(prefix, str)
            )
            or reference != resource["ownership"]["marker"]
        ):
            raise BudgetError("GCP resource reference is outside the exact task name scope")
    elif provider == "runpod":
        prefixes = scope.get("pod_name_prefixes")
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", reference)
            or not isinstance(prefixes, list)
            or not any(
                isinstance(prefix, str) and resource["ownership"]["marker"].startswith(prefix)
                for prefix in prefixes
            )
        ):
            raise BudgetError("RunPod resource reference is invalid for the task-scoped Pod")
    else:
        raise BudgetError("unsupported provider resource reference")
    return reference


def _cli_environment(executable: str) -> dict[str, str]:
    """Prepare a vendor CLI with isolated profile files and host-owned auth."""

    global _AZURE_CLI_READY
    if _CLI_CONFIG_ROOT is None:
        raise BudgetError(
            "cloud acceptance commands must run inside the Cloud Management container"
        )
    home = _CLI_CONFIG_ROOT / f"home-{executable}"
    home.mkdir(mode=0o700, exist_ok=True)
    environment = {"HOME": str(home), "LANG": "C.UTF-8", "PATH": "/usr/local/bin:/usr/bin:/bin"}
    if executable == "az":
        client_id = os.environ.get("AZURE_CLIENT_ID", "")
        subscription_id = os.environ.get("CYRENE_CLOUD_MGMT_AZURE_SUBSCRIPTION_ID", "")
        if not _AZURE_UUID_RE.fullmatch(client_id) or not _AZURE_UUID_RE.fullmatch(subscription_id):
            raise BudgetError("Azure CLI requires the pinned managed identity and subscription IDs")
        profile = next((row for row in _cloud_profiles() if row.get("provider") == "azure"), None)
        scope = profile.get("scope") if isinstance(profile, dict) else None
        if not isinstance(scope, dict) or scope.get("subscription_id") != subscription_id:
            raise BudgetError("Azure CLI subscription does not match the configured cloud profile")
        auth = profile.get("auth")
        if isinstance(auth, dict) and auth.get("client_id") not in {None, client_id}:
            raise BudgetError("Azure CLI identity does not match the configured cloud profile")
        environment["AZURE_CONFIG_DIR"] = str(_CLI_CONFIG_ROOT / "azure-config")
        environment["AZURE_CORE_COLLECT_TELEMETRY"] = "0"
        Path(environment["AZURE_CONFIG_DIR"]).mkdir(mode=0o700, exist_ok=True)
        if not _AZURE_CLI_READY:
            login = subprocess.run(
                ["az", "login", "--identity", "--client-id", client_id, "--output", "none"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=_CLI_CONFIG_ROOT,
                env=environment,
                timeout=120,
            )
            if login.returncode != 0:
                raise BudgetError("Azure managed identity CLI authentication failed")
            select = subprocess.run(
                ["az", "account", "set", "--subscription", subscription_id, "--output", "none"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=_CLI_CONFIG_ROOT,
                env=environment,
                timeout=60,
            )
            if select.returncode != 0:
                raise BudgetError("Azure CLI subscription selection failed")
            _AZURE_CLI_READY = True
    elif executable == "gcloud":
        profile = next((row for row in _cloud_profiles() if row.get("provider") == "gcp"), None)
        scope = profile.get("scope") if isinstance(profile, dict) else None
        credential_path = os.environ.get("CYRENE_CLOUD_MGMT_GCP_WIF_CONFIG", "")
        credential_file = Path(credential_path)
        if not isinstance(scope, dict) or not isinstance(scope.get("project_id"), str):
            raise BudgetError("GCP CLI project scope is unavailable")
        if not credential_path or credential_file.is_symlink() or not credential_file.is_file():
            raise BudgetError("GCP Workload Identity Federation config file is unavailable")
        credential_metadata = credential_file.stat()
        if credential_metadata.st_uid != os.geteuid() or credential_metadata.st_mode & 0o077:
            raise BudgetError("GCP Workload Identity Federation config file permissions are unsafe")
        environment.update(
            {
                "CLOUDSDK_CONFIG": str(_CLI_CONFIG_ROOT / "gcloud-config"),
                "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": credential_path,
                "CLOUDSDK_CORE_PROJECT": scope["project_id"],
                "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
                "CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK": "1",
            }
        )
        zones = scope.get("zones")
        if isinstance(zones, list) and zones and isinstance(zones[0], str):
            environment["CLOUDSDK_COMPUTE_ZONE"] = zones[0]
        Path(environment["CLOUDSDK_CONFIG"]).mkdir(mode=0o700, exist_ok=True)
    elif executable == "runpodctl":
        key_path = Path(os.environ.get("CYRENE_CLOUD_MGMT_RUNPOD_API_KEY_FILE", ""))
        if key_path.is_symlink() or not key_path.is_file():
            raise BudgetError("RunPod API key file is unavailable")
        metadata = key_path.stat()
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077 or metadata.st_size > 16_384:
            raise BudgetError("RunPod API key file permissions, owner, or size are unsafe")
        try:
            api_key = key_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as error:
            raise BudgetError("RunPod API key file is unreadable") from error
        if not api_key or "\x00" in api_key:
            raise BudgetError("RunPod API key file is empty or invalid")
        environment["RUNPOD_API_KEY"] = api_key
        environment["RUNPOD_CONFIG_DIR"] = str(_CLI_CONFIG_ROOT / "runpod-config")
        Path(environment["RUNPOD_CONFIG_DIR"]).mkdir(mode=0o700, exist_ok=True)
    else:
        raise BudgetError("unsupported cloud CLI executable")
    return environment


@contextlib.contextmanager
def _cloud_cli_state():
    """Create an isolated CLI home beneath the durable provider state volume."""

    global _AZURE_CLI_READY, _CLI_CONFIG_ROOT
    state_value = os.environ.get("CYRENE_CLOUD_MGMT_STATE_DIR", "")
    state_dir = Path(state_value)
    if (
        not state_value
        or not state_dir.is_absolute()
        or state_dir.is_symlink()
        or not state_dir.is_dir()
    ):
        raise BudgetError("Cloud Management durable state directory is unavailable")
    if state_dir.stat().st_uid != os.geteuid() or state_dir.stat().st_mode & 0o077:
        raise BudgetError("Cloud Management state directory permissions are unsafe")
    with tempfile.TemporaryDirectory(prefix="acceptance-cli-", dir=state_dir) as temporary:
        _CLI_CONFIG_ROOT = Path(temporary)
        _AZURE_CLI_READY = False
        try:
            yield
        finally:
            _CLI_CONFIG_ROOT = None
            _AZURE_CLI_READY = False


def _execute(argv: list[str], timeout: int) -> dict[str, Any]:
    """Run one approved CLI call without retaining provider output contents."""

    start = time.monotonic()
    if _CLI_CONFIG_ROOT is None:
        raise BudgetError(
            "cloud acceptance commands must run inside the Cloud Management container"
        )
    with (
        tempfile.TemporaryFile(mode="w+b", dir=_CLI_CONFIG_ROOT) as stdout_file,
        tempfile.TemporaryFile(mode="w+b", dir=_CLI_CONFIG_ROOT) as stderr_file,
    ):
        process = subprocess.Popen(
            argv,
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=_CLI_CONFIG_ROOT,
            env=_cli_environment(argv[0]),
            start_new_session=True,
            # This one-shot CLI runner is single-threaded and the hook only sets RLIMIT_FSIZE.
            preexec_fn=_limit_child_output,  # noqa: PLW1509
        )
        try:
            process.wait(timeout=timeout)
            outcome = "EXITED"
            code = process.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            outcome = "TIMED_OUT"
            code = None
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise
        stdout_bytes = stdout_file.tell()
        stderr_bytes = stderr_file.tell()
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(MAX_CAPTURE_BYTES + 1)
        stderr = stderr_file.read(MAX_CAPTURE_BYTES + 1)
        if stdout_bytes >= MAX_CAPTURE_BYTES or stderr_bytes >= MAX_CAPTURE_BYTES:
            outcome = "OUTPUT_LIMIT_EXCEEDED"
            code = None
    return {
        "outcome": outcome,
        "exitCode": code,
        "elapsedSeconds": round(time.monotonic() - start, 3),
        "stdoutBytes": stdout_bytes,
        "stderrBytes": stderr_bytes,
        "outputSha256": hashlib.sha256(stdout + b"\x00" + stderr).hexdigest(),
        "stdout": stdout,
    }


def _limit_child_output() -> None:
    """Cap each temporary stdout/stderr file before a provider CLI starts."""

    resource_limits.setrlimit(resource_limits.RLIMIT_FSIZE, (MAX_CAPTURE_BYTES, MAX_CAPTURE_BYTES))


def _argv(action: dict[str, Any], resource_ref: str | None = None) -> list[str]:
    """Expand the only supported cleanup substitution from an exact saved ID."""

    result = list(action["argv"])
    if resource_ref is not None:
        result = [part.replace("{resource_ref}", resource_ref) for part in result]
    return result


def _matches(
    document: Any, action: dict[str, Any], *, cleanup: bool = False
) -> list[dict[str, Any]]:
    """Filter list/object JSON by the configured exact task ownership marker."""

    items = _json_pointer(document, action["itemsPointer"])
    if isinstance(items, dict):
        candidates = [items]
    elif isinstance(items, list):
        candidates = [row for row in items if isinstance(row, dict)]
    else:
        raise BudgetError("provider read-back did not return an object or array")
    marker = action["markerValue"]
    matches = [row for row in candidates if _json_pointer(row, action["markerPointer"]) == marker]
    return matches


def _readback(
    resource: dict[str, Any], action_name: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one provider query and require structured JSON for exact reconciliation."""

    action = resource[action_name]
    argv = _argv(action)
    _validate_provider_action(
        {**action, "argv": argv},
        f"resource {resource['id']} {action_name}",
        resource["provider"],
        marker=resource["ownership"]["marker"],
        is_readback=True,
    )
    result = _execute(argv, action["timeoutSeconds"])
    event = {key: value for key, value in result.items() if key != "stdout"}
    if result["outcome"] != "EXITED" or result["exitCode"] != 0:
        raise BudgetError(f"{resource['id']} {action_name} did not complete; state remains UNKNOWN")
    try:
        document = json.loads(result["stdout"])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BudgetError(f"{resource['id']} {action_name} returned non-JSON output") from error
    if action_name == "cleanupReadback":
        items = _json_pointer(document, action["itemsPointer"])
        if isinstance(items, dict):
            candidates = [items]
        elif isinstance(items, list):
            candidates = [row for row in items if isinstance(row, dict)]
        else:
            raise BudgetError("cleanup verification did not return an object or array")
        matches = [
            row
            for row in candidates
            if _json_pointer(row, action["markerPointer"]) == action["markerValue"]
        ]
        return matches, event
    return _matches(document, action), event


def _event(ledger: dict[str, Any], resource_id: str, name: str, facts: dict[str, Any]) -> None:
    """Append a content-free evidence record and refresh the UTC timestamp."""

    record = ledger["resources"][resource_id]
    record.setdefault("events", []).append(
        {"action": name, "at": datetime.now(UTC).isoformat(), **facts}
    )


def _create_one(resource: dict[str, Any], ledger: dict[str, Any], ledger_path: Path) -> None:
    """Create at most once and persist the exact owner-tagged vendor reference."""

    resource_id = resource["id"]
    entry = ledger["resources"][resource_id]
    if entry["state"] != "PLANNED":
        raise BudgetError(
            f"{resource_id} has prior state {entry['state']}; cleanup and read-back are required first"
        )
    matches, facts = _readback(resource, "readback")
    _event(ledger, resource_id, "read-back-before-create", facts)
    if matches:
        if len(matches) == 1:
            entry["vendorResourceRef"] = _validate_resource_reference(
                resource,
                _json_pointer(matches[0], resource["readback"]["referencePointer"]),
            )
            entry["state"] = "CREATED"
            _atomic_write(ledger_path, ledger)
            raise BudgetError(
                f"{resource_id} already exists under this owner marker; it will be cleaned up without recreation"
            )
        entry["state"] = "UNKNOWN"
        _atomic_write(ledger_path, ledger)
        raise BudgetError(
            f"{resource_id} owner marker matched multiple resources; automated cleanup stopped"
        )

    entry["state"] = "CREATE_STARTED"
    _atomic_write(ledger_path, ledger)
    result = _execute(_argv(resource["create"]), resource["create"]["timeoutSeconds"])
    _event(
        ledger,
        resource_id,
        "create",
        {key: value for key, value in result.items() if key != "stdout"},
    )
    entry["state"] = "UNKNOWN"
    _atomic_write(ledger_path, ledger)
    matches, facts = _readback(resource, "readback")
    _event(ledger, resource_id, "read-back-after-create", facts)
    if len(matches) == 1:
        entry["vendorResourceRef"] = _validate_resource_reference(
            resource,
            _json_pointer(matches[0], resource["readback"]["referencePointer"]),
        )
        if not isinstance(entry["vendorResourceRef"], str) or not entry["vendorResourceRef"]:
            _atomic_write(ledger_path, ledger)
            raise BudgetError(
                f"{resource_id} read-back returned an empty vendor resource reference"
            )
        entry["state"] = "CREATED"
        _atomic_write(ledger_path, ledger)
    elif len(matches) > 1:
        _atomic_write(ledger_path, ledger)
        raise BudgetError(
            f"{resource_id} owner marker matched multiple resources; automated cleanup stopped"
        )
    else:
        _atomic_write(ledger_path, ledger)
        raise BudgetError(f"{resource_id} was not observed after create; it will not be recreated")
    if result["outcome"] != "EXITED" or result["exitCode"] != 0:
        raise BudgetError(
            f"{resource_id} create timed out or returned failure; exact read-back is saved for cleanup"
        )


def _readback_step(
    resource: dict[str, Any], step: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run an operation-specific query and return only owner-marker matches."""

    action = step["readback"]
    argv = _argv(action)
    _validate_provider_action(
        {**action, "argv": argv},
        f"resource {resource['id']} exercise readback",
        resource["provider"],
        marker=resource["ownership"]["marker"],
        is_readback=True,
    )
    result = _execute(argv, action["timeoutSeconds"])
    event = {key: value for key, value in result.items() if key != "stdout"}
    if result["outcome"] != "EXITED" or result["exitCode"] != 0:
        raise BudgetError(
            f"{resource['id']} exercise read-back failed; resource state remains UNKNOWN"
        )
    try:
        document = json.loads(result["stdout"])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BudgetError(
            f"{resource['id']} exercise read-back returned non-JSON output"
        ) from error
    return _matches(document, action), event


def _run_exercises(resource: dict[str, Any], ledger: dict[str, Any], ledger_path: Path) -> None:
    """Run bounded calls and require exact provider-state read-back after each."""

    resource_id = resource["id"]
    entry = ledger["resources"][resource_id]
    resource_ref = entry.get("vendorResourceRef")
    if not isinstance(resource_ref, str) or not resource_ref:
        raise BudgetError(f"{resource_id} has no saved vendor reference for acceptance operations")
    _validate_resource_reference(resource, resource_ref)
    for step in resource["exercise"]:
        operation = step["action"]
        argv = _argv(operation, resource_ref)
        _validate_provider_action(
            {"argv": argv, "timeoutSeconds": operation["timeoutSeconds"]},
            f"resource {resource_id} exercise {step['name']}",
            resource["provider"],
        )
        result = _execute(argv, operation["timeoutSeconds"])
        _event(
            ledger,
            resource_id,
            f"exercise:{step['name']}",
            {key: value for key, value in result.items() if key != "stdout"},
        )
        _atomic_write(ledger_path, ledger)
        matches, facts = _readback_step(resource, step)
        _event(ledger, resource_id, f"exercise-readback:{step['name']}", facts)
        _atomic_write(ledger_path, ledger)
        if len(matches) != 1:
            raise BudgetError(
                f"{resource_id} exercise {step['name']} did not identify exactly one owned resource"
            )
        if (
            _json_pointer(matches[0], step["readback"]["expectedPointer"])
            != step["readback"]["expectedValue"]
        ):
            raise BudgetError(
                f"{resource_id} exercise {step['name']} failed its exact state read-back"
            )
        if result["outcome"] != "EXITED" or result["exitCode"] != 0:
            raise BudgetError(
                f"{resource_id} exercise {step['name']} timed out or returned failure after read-back"
            )


def run_bounded(plan: dict[str, Any], ledger: dict[str, Any], ledger_path: Path) -> None:
    """Create, exercise, and always reconcile/delete each task-owned resource."""

    states = {key: row["state"] for key, row in ledger["resources"].items()}
    if any(state != "PLANNED" for state in states.values()):
        raise BudgetError(
            "ledger contains prior resource state; reconcile and clean it before a new acceptance run"
        )
    failure: BaseException | None = None
    try:
        for resource in plan["resources"]:
            _create_one(resource, ledger, ledger_path)
            _run_exercises(resource, ledger, ledger_path)
            cleanup_resources({"resources": [resource]}, ledger, ledger_path)
    except BaseException as error:  # noqa: BLE001 - cancellation must enter the cleanup path.
        failure = error
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    for signum in previous_handlers:
        signal.signal(signum, signal.SIG_IGN)
    try:
        cleanup_resources(plan, ledger, ledger_path)
    except BaseException as cleanup_error:
        if failure is not None:
            raise BudgetError(
                f"acceptance stopped ({type(failure).__name__}); automatic cleanup is incomplete and needs reconciliation"
            ) from cleanup_error
        raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    if failure is not None:
        raise failure


def _cleanup_resource(resource: dict[str, Any], ledger: dict[str, Any], ledger_path: Path) -> None:
    """Reconcile and delete one exact owner-tagged resource."""

    resource_id = resource["id"]
    entry = ledger["resources"][resource_id]
    if entry["state"] == "DELETED":
        return
    uncertain_states = {"UNKNOWN", "CREATE_STARTED", "CLEANUP_STARTED", "CLEANUP_UNKNOWN"}
    if entry["state"] in uncertain_states:
        matches, facts = _readback(resource, "readback")
        _event(ledger, resource_id, "reconcile-before-cleanup", facts)
        if not matches:
            absent, absence_facts = _readback(resource, "cleanupReadback")
            _event(ledger, resource_id, "read-back-after-reconcile", absence_facts)
            if absent:
                entry["state"] = "CLEANUP_UNKNOWN"
                _atomic_write(ledger_path, ledger)
                raise BudgetError(
                    f"{resource_id} final absence read-back still found an owned resource"
                )
            entry["state"] = "DELETED"
            _atomic_write(ledger_path, ledger)
            return
        if len(matches) > 1:
            entry["state"] = "UNKNOWN"
            _atomic_write(ledger_path, ledger)
            raise BudgetError(f"{resource_id} read-back is ambiguous; automated deletion stopped")
        entry["vendorResourceRef"] = _validate_resource_reference(
            resource,
            _json_pointer(matches[0], resource["readback"]["referencePointer"]),
        )
    if entry["state"] not in {"CREATED", *uncertain_states}:
        return
    resource_ref = entry.get("vendorResourceRef")
    if not isinstance(resource_ref, str) or not resource_ref:
        entry["state"] = "UNKNOWN"
        _atomic_write(ledger_path, ledger)
        raise BudgetError(
            f"{resource_id} has no exact resource reference; automated cleanup stopped"
        )
    _validate_resource_reference(resource, resource_ref)
    cleanup_argv = _argv(resource["cleanup"], resource_ref)
    _validate_provider_action(
        {"argv": cleanup_argv, "timeoutSeconds": resource["cleanup"]["timeoutSeconds"]},
        f"resource {resource_id} cleanup",
        resource["provider"],
    )
    entry["state"] = "CLEANUP_STARTED"
    _atomic_write(ledger_path, ledger)
    result = _execute(cleanup_argv, resource["cleanup"]["timeoutSeconds"])
    _event(
        ledger,
        resource_id,
        "cleanup",
        {key: value for key, value in result.items() if key != "stdout"},
    )
    entry["state"] = "CLEANUP_UNKNOWN"
    _atomic_write(ledger_path, ledger)
    matches, facts = _readback(resource, "cleanupReadback")
    _event(ledger, resource_id, "read-back-after-cleanup", facts)
    if matches:
        _atomic_write(ledger_path, ledger)
        raise BudgetError(f"{resource_id} still exists after cleanup; it remains recorded")
    entry["state"] = "DELETED"
    _atomic_write(ledger_path, ledger)


def cleanup_resources(plan: dict[str, Any], ledger: dict[str, Any], ledger_path: Path) -> None:
    """Attempt cleanup for every resource, then report all unresolved references."""

    failures: list[str] = []
    for resource in reversed(plan["resources"]):
        try:
            _cleanup_resource(resource, ledger, ledger_path)
        except Exception:  # noqa: BLE001 - one provider failure must not block sibling cleanup.
            failures.append(resource["id"])
    if failures:
        raise BudgetError(
            "automated cleanup remains incomplete for resource IDs: " + ", ".join(failures)
        )


def _verify_cleanup(plan: dict[str, Any], ledger: dict[str, Any], ledger_path: Path) -> None:
    """Repeat final provider absence queries before reporting resources reclaimed."""

    failures: list[str] = []
    for resource in plan["resources"]:
        entry = ledger["resources"][resource["id"]]
        if entry["state"] != "DELETED":
            failures.append(resource["id"])
            continue
        try:
            matches, facts = _readback(resource, "cleanupReadback")
            _event(ledger, resource["id"], "verify-cleanup-absence", facts)
            if matches:
                entry["state"] = "CLEANUP_UNKNOWN"
                failures.append(resource["id"])
            _atomic_write(ledger_path, ledger)
        except Exception:  # noqa: BLE001 - continue final checks for every resource.
            failures.append(resource["id"])
    if failures:
        raise BudgetError("fresh cleanup read-back failed for resource IDs: " + ", ".join(failures))


@contextlib.contextmanager
def _ledger_lock(path: Path):
    """Serialize one acceptance task and reject lock-file symlink substitution."""

    lock_path = path.with_name(path.name + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid():
            raise BudgetError("acceptance lock permissions or owner are unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as error:
        raise BudgetError("another acceptance process already holds this task ledger") from error
    finally:
        os.close(descriptor)


def main() -> int:
    """Preflight, run, clean, or verify a bounded resource acceptance plan."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "run", "cleanup", "verify-cleanup"):
        command = commands.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--ledger", type=Path, required=True)
        if name in {"preflight", "run"}:
            command.add_argument("--previous-evidence", type=Path, required=True)
        if name in {"run", "cleanup", "verify-cleanup"}:
            command.add_argument(
                "--execute", action="store_true", help="required to invoke vendor CLIs"
            )
    arguments = parser.parse_args()
    try:
        if not _is_private_artifact(arguments.ledger):
            raise BudgetError(
                "acceptance ledgers must be stored under the umbrella .artifacts directory"
            )
        previous = (
            arguments.previous_evidence.resolve()
            if hasattr(arguments, "previous_evidence")
            else None
        )
        plan = validate_plan(
            arguments.plan.resolve(),
            previous_evidence=previous,
            for_cleanup=arguments.command in {"cleanup", "verify-cleanup"},
        )
        if arguments.command == "preflight":
            amount = sum(float(row["pricing"]["upperBoundUsd"]) for row in plan["resources"])
            previous_amount = _read_previous_evidence(
                previous,
                expected_stage="github_hf" if plan["stage"] == "azure_gcp" else "azure_gcp",
                task_id=plan["taskId"],
                owner=plan["owner"],
            )[1]
            print(
                json.dumps(
                    {
                        "status": "READY",
                        "phaseReservedUsd": round(amount, 2),
                        "previousReservedUsd": round(previous_amount, 2),
                        "cumulativeReservedUsd": round(amount + previous_amount, 2),
                        "resources": len(plan["resources"]),
                    }
                )
            )
            return 0
        if (
            arguments.command in {"cleanup", "verify-cleanup"}
            and not arguments.ledger.resolve().is_file()
        ):
            raise BudgetError("cleanup requires the existing private acceptance ledger")
        if not arguments.execute:
            raise BudgetError(f"{arguments.command} requires --execute to invoke provider CLIs")
        with _cloud_cli_state(), _ledger_lock(arguments.ledger.resolve()):
            previous_amount = 0.0
            previous_evidence_sha256 = None
            if arguments.command == "run":
                previous_amount = _read_previous_evidence(
                    previous,
                    expected_stage="github_hf" if plan["stage"] == "azure_gcp" else "azure_gcp",
                    task_id=plan["taskId"],
                    owner=plan["owner"],
                )[1]
                previous_evidence_sha256 = hashlib.sha256(previous.read_bytes()).hexdigest()
            ledger = _load_ledger(
                arguments.ledger.resolve(),
                plan,
                previous_reserved=previous_amount,
                previous_evidence_sha256=previous_evidence_sha256,
            )
            if arguments.command == "run":
                _atomic_write(arguments.ledger.resolve(), ledger)
                previous_handler = signal.getsignal(signal.SIGTERM)
                signal.signal(
                    signal.SIGTERM,
                    lambda _signum, _frame: (_ for _ in ()).throw(KeyboardInterrupt()),
                )
                try:
                    try:
                        run_bounded(plan, ledger, arguments.ledger.resolve())
                    except BaseException:
                        ledger["status"] = (
                            "FAILED_CLEANED"
                            if all(
                                row["state"] in {"PLANNED", "DELETED"}
                                for row in ledger["resources"].values()
                            )
                            else "CLEANUP_INCOMPLETE"
                        )
                        _atomic_write(arguments.ledger.resolve(), ledger)
                        raise
                    try:
                        _verify_cleanup(plan, ledger, arguments.ledger.resolve())
                    except BaseException:
                        ledger["status"] = "CLEANUP_INCOMPLETE"
                        _atomic_write(arguments.ledger.resolve(), ledger)
                        raise
                    ledger["status"] = "VERIFIED"
                    ledger["verifiedAt"] = datetime.now(UTC).isoformat()
                    ledger["cleanupVerifiedAt"] = ledger["verifiedAt"]
                    _atomic_write(arguments.ledger.resolve(), ledger)
                finally:
                    signal.signal(signal.SIGTERM, previous_handler)
                print(
                    json.dumps(
                        {
                            "status": ledger["status"],
                            "taskId": ledger["taskId"],
                            "stage": ledger["stage"],
                            "reservedUsd": ledger["reservedUsd"],
                            "cumulativeReservedUsd": ledger["cumulativeReservedUsd"],
                        }
                    )
                )
                return 0
            if arguments.command == "cleanup":
                ledger["status"] = "CLEANUP_IN_PROGRESS"
                _atomic_write(arguments.ledger.resolve(), ledger)
                try:
                    cleanup_resources(plan, ledger, arguments.ledger.resolve())
                except BaseException:
                    ledger["status"] = "CLEANUP_INCOMPLETE"
                    _atomic_write(arguments.ledger.resolve(), ledger)
                    raise
                ledger["status"] = "CLEANED"
                ledger["cleanedAt"] = datetime.now(UTC).isoformat()
                _atomic_write(arguments.ledger.resolve(), ledger)
                print("all task resources are deleted and absent by provider read-back")
                return 0
            try:
                _verify_cleanup(plan, ledger, arguments.ledger.resolve())
            except BaseException:
                ledger["status"] = "CLEANUP_INCOMPLETE"
                _atomic_write(arguments.ledger.resolve(), ledger)
                raise
            if ledger.get("status") != "VERIFIED":
                ledger["status"] = "CLEANED"
            ledger["cleanupVerifiedAt"] = datetime.now(UTC).isoformat()
            _atomic_write(arguments.ledger.resolve(), ledger)
            print("fresh provider queries confirm every task resource is absent")
            return 0
    except KeyboardInterrupt:
        print("Cloud acceptance cancelled; cleanup reconciliation was attempted.", file=sys.stderr)
        return 130
    except (OSError, BudgetError, subprocess.SubprocessError) as error:
        print(f"Cloud acceptance blocked: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
