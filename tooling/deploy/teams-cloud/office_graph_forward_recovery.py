"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 office_graph_forward_recovery.py                                 │
│  Module: tooling.deploy.teams_cloud.office_graph_forward_recovery    │
│  Role: Build and apply one immutable forward continuation for the    │
│        interrupted Office Graph bootstrap.                           │
│                                                                      │
│  模块职责：为中断的 Office Graph 引导创建并执行一次性前向续跑计划。   │
│  · 复核冻结来源与实时身份  · 不确定时停止并记录恢复状态              │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import account_center_core_recovery as core_recovery
import stage_transition as st

SCHEMA = "cyrene-office-graph-forward-recovery-v1"
RECOVERY_JOURNAL_SCHEMA = f"{SCHEMA}-journal"
CORE_RECOVERY_JOURNAL_SCHEMA = "cyrene-account-center-core-recovery-v1-journal"
CORE_RESTORED_STATE = "CORE_RESTORED_MIXED_PENDING_NEW_PLAN"
FORWARD_APPLIED_STATE = "OFFICE_GRAPH_FORWARD_APPLIED"
ORIGINAL_OPERATION_ID = "5583effd-c4ad-419f-b44c-5a33ee48dd24"
ORIGINAL_PLAN_SHA256 = "c3873db692157e1909e5869e7ed64efeaa563b69b6af5cefd73877adc6b74eba"
ORIGINAL_JOURNAL_SHA256 = "d30d20f26d31c6d70a2e6e5945cfd97ad62711bd6c4c6b1ab0c3d69b8bce7b09"
CORE_RECOVERY_OPERATION_ID = "1d65d751-2999-447b-b429-560f3c0424c2"
CORE_RECOVERY_PLAN_SHA256 = "9773b2cb6b4db870bbf6333f62bb954056e9a55444f63052abdcea71ff18c685"
FIXED_ACCOUNT_CENTER_IMAGE_REF = (
    "cyrene/account-center:office-ac-migration-fix-20261030-6d84dc802e81"
)
FIXED_ACCOUNT_CENTER_IMAGE_ID = (
    "sha256:4d5bdb3807b1cab51e114e08b713a3972e7b396f0133d0e923254b4b0b643203"
)
OLD_ACCOUNT_CENTER_IMAGE_ID = (
    "sha256:13165b21b27c380cf9e62732bbba68afb30464e4256023cfb974c0dc442e5968"
)
FIXED_PLUGINS_COMMIT = "6d84dc802e8175226ca2d04eb73ada3b8e46ba98"
TARGETS = tuple(
    target
    for target in st.OFFICE_GRAPH_STAGE_TARGET_ORDER
    if target != st.PERSISTENCE_IMAGE_UPDATE_TARGET
)
MAX_RUNTIME_SERVICES = 15
MAX_MEMORY_MIB = 3584
MAX_CPUS = 1.95
MINIMUM_MEM_AVAILABLE_MIB = 1024
REQUIRED_FREE_GIB = 2.0


class ForwardRecoveryError(ValueError):
    """Raised when the closed continuation cannot be proven safe to apply."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ForwardRecoveryError(message)


def _digest(value: Any) -> str:
    return hashlib.sha256(st._canonical_json(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _private_regular(path: Path, label: str, *, expected_uid: int | None = None) -> None:
    """Require one current-user-owned private artifact file without symlinks."""

    try:
        metadata = path.lstat()
    except OSError as error:
        raise ForwardRecoveryError(f"{label} is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != (os.geteuid() if expected_uid is None else expected_uid)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > 16 * 1024 * 1024
        or not st.preflight._private_artifact(path)
    ):
        raise ForwardRecoveryError(f"{label} must be an owner-only mode 0600 artifact")


def _private_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ForwardRecoveryError(f"{label} directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or path.resolve(strict=True) != path
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ForwardRecoveryError(f"{label} directory must be owner-only mode 0700")


def _private_readonly_snapshot(path: Path, label: str) -> None:
    """Accept only a root-owned, read-only mode 0400 SQLite snapshot artifact."""

    try:
        metadata = path.lstat()
    except OSError as error:
        raise ForwardRecoveryError(f"{label} is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o400
        or metadata.st_size > 512 * 1024 * 1024
        or not st.preflight._private_artifact(path)
    ):
        raise ForwardRecoveryError(f"{label} must be a root-owned, mode 0400 read-only artifact")


def _read_private_json(path: Path, label: str) -> dict[str, Any]:
    _private_regular(path, label)
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ForwardRecoveryError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ForwardRecoveryError(f"{label} must be a JSON object")
    return value


def _file_record(
    path: Path, label: str, *, expected_uid: int | None = None
) -> dict[str, str]:
    _private_regular(path, label, expected_uid=expected_uid)
    resolved = path.resolve(strict=True)
    return {"path": str(resolved), "sha256": _sha256_file(resolved)}


def _verify_file_record(
    record: Any, label: str, *, expected_uid: int | None = None
) -> Path:
    if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
        raise ForwardRecoveryError(f"{label} plan reference is malformed")
    path_text = record.get("path")
    digest = record.get("sha256")
    if (
        not isinstance(path_text, str)
        or not isinstance(digest, str)
        or st.SHA256_RE.fullmatch(digest) is None
    ):
        raise ForwardRecoveryError(f"{label} plan reference is malformed")
    path = Path(path_text)
    if _file_record(path, label, expected_uid=expected_uid) != record:
        raise ForwardRecoveryError(f"{label} changed after the continuation was sealed")
    return path


def _write_exclusive(path: Path, document: dict[str, Any]) -> str:
    """Persist a private plan once; an existing plan is never overwritten."""

    _private_directory(path.parent, "forward plan")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    payload = st._canonical_json(document) + b"\n"
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise ForwardRecoveryError("forward plan already exists; choose a new operation") from error
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as error:
        raise ForwardRecoveryError("forward plan could not be durably written") from error
    return hashlib.sha256(payload).hexdigest()


def _implementation_sha256() -> str:
    """Bind this continuation and the canonical executor/recovery helpers."""

    return _digest(
        [
            {
                "path": str(Path(path).resolve(strict=True)),
                "sha256": _sha256_file(Path(path).resolve(strict=True)),
            }
            for path in (
                __file__,
                st.__file__,
                core_recovery.__file__,
                st.runtime_update.__file__,
                st.preflight.__file__,
            )
        ]
    )


def _validate_original_failure(
    source: dict[str, Any], journal: dict[str, Any], journal_sha256: str
) -> None:
    """Accept only the named failed Graph plan with P applied and AC unresolved."""

    _require(
        source.get("schemaVersion") == st.PLAN_SCHEMA
        and source.get("operationId") == ORIGINAL_OPERATION_ID
        and source.get("planSha256") == ORIGINAL_PLAN_SHA256
        and source.get("phase") == st.OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE,
        "source plan is not the exact interrupted Office Graph bootstrap",
    )
    active_account_center = (
        source.get("candidateServices", {}).get("account-center", {}).get("active")
    )
    _require(
        isinstance(active_account_center, dict)
        and active_account_center.get("imageId") == OLD_ACCOUNT_CENTER_IMAGE_ID,
        "source plan does not pin the restored original Account Center image",
    )
    _require(
        source.get("targets") == list(st.OFFICE_GRAPH_STAGE_TARGET_ORDER),
        "source plan target order is outside the supported Graph bootstrap",
    )
    _require(
        journal_sha256 == ORIGINAL_JOURNAL_SHA256
        and journal.get("schemaVersion") == st.JOURNAL_SCHEMA
        and journal.get("operationId") == ORIGINAL_OPERATION_ID
        and journal.get("planSha256") == ORIGINAL_PLAN_SHA256
        and journal.get("planPath") == source.get("planPath", journal.get("planPath"))
        and journal.get("state") == "RECOVERY_REQUIRED"
        and journal.get("lastErrorCode") == st.ACCOUNT_CENTER_DOWNGRADE_BLOCKED_ERROR,
        "source journal is not the frozen Account Center schema downgrade failure",
    )
    progress = journal.get("targetProgress")
    _require(
        isinstance(progress, dict)
        and set(progress) == set(st.OFFICE_GRAPH_STAGE_TARGET_ORDER),
        "source target journal is not the closed Graph bootstrap set",
    )
    persistence = progress[st.PERSISTENCE_IMAGE_UPDATE_TARGET]
    _require(
        persistence.get("phase") == "APPLIED"
        and persistence.get("applyIntent") is False
        and persistence.get("candidateContainerId") == persistence.get("healthyContainerId"),
        "source Persistence target was not fully applied",
    )
    account_center = progress["account-center"]
    _require(
        account_center.get("phase") == "CONTAINER_READBACK"
        and account_center.get("applyIntent") is True
        and account_center.get("rollbackIntent") is False,
        "source Account Center replacement is outside the supported failed state",
    )
    for service in st.OFFICE_GRAPH_STAGE_TARGET_ORDER:
        if service in {st.PERSISTENCE_IMAGE_UPDATE_TARGET, "account-center"}:
            continue
        row = progress[service]
        _require(
            row.get("phase") == "PREPARED"
            and row.get("applyIntent") is False
            and row.get("candidateContainerId") is None,
            "a later Graph bootstrap target may already have changed",
        )
    selection = journal.get("selection")
    _require(
        isinstance(selection, dict)
        and selection.get("promoted") is False
        and selection.get("promotionIntent") is False,
        "the original selector promotion may have started",
    )


def _validate_core_recovery_journal(
    document: dict[str, Any], source_journal_sha256: str
) -> dict[str, Any]:
    """Require the exact independent AC restore and normalized 14-service readback."""

    _require(
        document.get("schemaVersion") == CORE_RECOVERY_JOURNAL_SCHEMA
        and document.get("operationId") == CORE_RECOVERY_OPERATION_ID
        and document.get("planSha256") == CORE_RECOVERY_PLAN_SHA256
        and document.get("state") == CORE_RESTORED_STATE
        and document.get("sourceOperationId") == ORIGINAL_OPERATION_ID
        and document.get("sourcePlanSha256") == ORIGINAL_PLAN_SHA256
        and document.get("sourceJournalSha256") == source_journal_sha256
        and source_journal_sha256 == ORIGINAL_JOURNAL_SHA256,
        "independent core recovery journal does not bind the exact failed source",
    )
    receipt = document.get("coreRecovery")
    _require(
        isinstance(receipt, dict)
        and receipt.get("state") == CORE_RESTORED_STATE
        and receipt.get("databaseSchemaVersion") == 8
        and receipt.get("databaseRestorePerformed") is False
        and receipt.get("profileIdentitiesPreserved") is True
        and receipt.get("originalSelectorUnchanged") is True
        and receipt.get("officeEnabled") is False
        and receipt.get("deliveryPaused") is False
        and receipt.get("deadCount") == 0,
        "core recovery receipt is not the exact healthy mixed state",
    )
    containers = receipt.get("runtimeContainers")
    _require(
        isinstance(containers, dict) and set(containers) == set(st.RUNTIME_SERVICE_SET),
        "core recovery runtime map is not the exact active 14-service set",
    )
    for name, identity in containers.items():
        _require(
            isinstance(identity, dict)
            and set(identity)
            == {"id", "imageId", "configSha256", "mountsSha256", "hostConfigSha256"}
            and isinstance(identity.get("id"), str)
            and st.FULL_CONTAINER_ID_RE.fullmatch(identity["id"]) is not None
            and isinstance(identity.get("imageId"), str)
            and st.IMAGE_ID_RE.fullmatch(identity["imageId"]) is not None
            and all(
                isinstance(identity.get(field), str)
                and st.SHA256_RE.fullmatch(identity[field]) is not None
                for field in ("configSha256", "mountsSha256", "hostConfigSha256")
            ),
            f"core recovery identity for {name} is malformed",
        )
    _require(
        receipt.get("retainedPersistence") == containers[st.PERSISTENCE_IMAGE_UPDATE_TARGET],
        "core recovery did not preserve the exact candidate Persistence container",
    )
    restored_account_center = receipt.get("restoredAccountCenter")
    _require(
        isinstance(restored_account_center, dict)
        and restored_account_center.get("containerId")
        == containers["account-center"]["id"]
        and restored_account_center.get("imageId")
        == containers["account-center"]["imageId"]
        and restored_account_center.get("health") == "healthy",
        "core recovery Account Center identity is not the restored live container",
    )
    return receipt


def _validate_migration_evidence(
    evidence: dict[str, Any], database_evidence: dict[str, Any]
) -> None:
    """Bind offline ARM64 v8-to-v9 replay to this snapshot and fixed image."""

    migration = evidence.get("migration")
    _require(
        evidence.get("state") == "FIXED_ACCOUNT_CENTER_REAL_SNAPSHOT_MIGRATION_PASS"
        and evidence.get("deployed") is False
        and evidence.get("activeContainerIdsUnchanged") is True
        and evidence.get("liveDatabaseMounted") is False
        and evidence.get("secretsMounted") is False
        and evidence.get("network") == "none"
        and evidence.get("platform") == "linux/arm64"
        and evidence.get("imageId") == FIXED_ACCOUNT_CENTER_IMAGE_ID
        and evidence.get("pluginsCommit") == FIXED_PLUGINS_COMMIT
        and evidence.get("snapshotSha256") == database_evidence.get("snapshotSha256")
        and isinstance(evidence.get("imageArchiveSha256"), str)
        and st.SHA256_RE.fullmatch(evidence["imageArchiveSha256"]) is not None,
        "fixed Account Center migration proof is not bound to the isolated source snapshot",
    )
    _require(
        isinstance(migration, dict)
        and migration.get("fromVersion") == 8
        and migration.get("toVersion") == 9
        and migration.get("allPreexistingColumnsAndRowsPreserved") is True
        and migration.get("foreignKeyViolationCount") == 0
        and migration.get("quickCheck") == ["ok"]
        and migration.get("secondInitializationPassed") is True,
        "fixed Account Center image failed the isolated repeated migration gate",
    )


def _validate_fixed_override(override: dict[str, Any]) -> None:
    """Allow only the fixed Account Center image reference in the Compose overlay."""

    services = override.get("services")
    _require(
        set(override) == {"services"}
        and isinstance(services, dict)
        and set(services) == {"account-center"}
        and isinstance(services["account-center"], dict)
        and set(services["account-center"]) == {"image"}
        and services["account-center"].get("image") == FIXED_ACCOUNT_CENTER_IMAGE_REF,
        "fixed Account Center Compose override changes fields beyond its exact image",
    )


def _validate_fixed_lock_row(row: Any) -> None:
    _require(
        isinstance(row, dict)
        and row.get("service") == "account-center"
        and row.get("image") == FIXED_ACCOUNT_CENTER_IMAGE_REF
        and row.get("imageId") == FIXED_ACCOUNT_CENTER_IMAGE_ID
        and row.get("platform") == "linux/arm64"
        and row.get("sourceCommits", {}).get("plugins") == FIXED_PLUGINS_COMMIT,
        "candidate image lock does not contain the fixed migrated ARM64 Account Center image",
    )


def _validate_candidate_lock_delta(
    original: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Allow the exact AC row, assembly metadata, and provenance receipt delta."""

    original_rows = st.runtime_update._image_rows_by_service(original)
    candidate_rows = st.runtime_update._image_rows_by_service(candidate)
    _require(
        set(original_rows) == set(candidate_rows),
        "candidate image lock changed its built service inventory",
    )
    _validate_fixed_lock_row(candidate_rows.get("account-center"))
    normalized = copy.deepcopy(original)
    for index, row in enumerate(normalized["images"]):
        if row.get("service") == "account-center":
            normalized["images"][index] = candidate_rows["account-center"]
            break
    source_extension = {
        "remote": "https://github.com/DoHorizon-AI/Cyrene-Plugins-Official.git",
        "commit": FIXED_PLUGINS_COMMIT,
        "tree": "b02d8b39ae0b104fb08ee144b62a14fedd4da355",
    }
    candidate_sources = candidate.get("sources")
    original_sources = original.get("sources")
    _require(
        isinstance(candidate_sources, dict)
        and isinstance(original_sources, dict)
        and candidate_sources.get("plugins-account-center-fix") == source_extension
        and set(candidate_sources) == set(original_sources) | {"plugins-account-center-fix"},
        "candidate lock lacks exact fixed Account Center source provenance",
    )
    _require(
        all(candidate_sources[name] == row for name, row in original_sources.items()),
        "candidate lock changed an existing source provenance row",
    )
    normalized["sources"] = copy.deepcopy(candidate_sources)
    for field in ("taskId", "createdAt", "planSha256"):
        if field in original or field in candidate:
            value = candidate.get(field)
            _require(
                isinstance(value, str)
                and bool(value)
                and (field != "planSha256" or st.SHA256_RE.fullmatch(value) is not None),
                f"candidate image lock {field} metadata is malformed",
            )
            normalized[field] = value
    _require(
        normalized == candidate,
        "candidate lock changes rows, provenance, or metadata beyond fixed Account Center replacement",
    )
    return candidate_rows["account-center"]


def _validate_source_database_evidence(
    evidence: dict[str, Any], snapshot: dict[str, Any], live: dict[str, Any], database_path: Path
) -> dict[str, Any]:
    """Bind source schema/profile ownership while allowing lease rows to evolve."""

    snapshot_path_text = evidence.get("snapshotPath")
    _require(
        evidence.get("state") == "REAL_READONLY_ACCOUNT_CENTER_FORWARD_SNAPSHOT"
        and evidence.get("liveDatabaseMutated") is False
        and evidence.get("databasePath") == str(database_path)
        and evidence.get("userVersion") == 8
        and isinstance(evidence.get("profilesIdentitySha256"), str)
        and st.SHA256_RE.fullmatch(evidence["profilesIdentitySha256"]) is not None
        and evidence.get("foreignKeyViolationCount") == 0
        and evidence.get("quickCheck") == ["ok"]
        and isinstance(snapshot_path_text, str),
        "read-only Account Center source database receipt is malformed",
    )
    _require(
        snapshot.get("userVersion") == 8
        and snapshot.get("schemaSha256") == evidence.get("schemaSha256")
        and snapshot.get("tables") == evidence.get("tables")
        and snapshot.get("providers") == evidence.get("providers")
        and snapshot.get("profilesIdentitySha256") == evidence.get("profilesIdentitySha256")
        and snapshot.get("foreignKeyViolationCount") == 0
        and snapshot.get("quickCheck") == ["ok"],
        "source SQLite snapshot differs from its read-only reconciliation receipt",
    )
    _require(
        live.get("userVersion") == 8
        and live.get("schemaSha256") == snapshot.get("schemaSha256")
        and live.get("foreignKeyViolationCount") == 0
        and live.get("quickCheck") == ["ok"]
        and live.get("profilesIdentitySha256") == snapshot.get("profilesIdentitySha256")
        and live.get("providers") == snapshot.get("providers")
        and all(row.get("provider") != "microsoft_graph" for row in live.get("providers", [])),
        "live Account Center schema, owner/scope profiles, or pre-authorization state changed",
    )
    return {
        "databasePath": str(database_path),
        "userVersion": live["userVersion"],
        "schemaSha256": live["schemaSha256"],
        "profilesIdentitySha256": live["profilesIdentitySha256"],
        "providers": live["providers"],
        "foreignKeyViolationCount": live["foreignKeyViolationCount"],
        "quickCheck": live["quickCheck"],
        "snapshotSha256": evidence["snapshotSha256"],
    }


def _validate_migrated_database(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Check v9 integrity and protected profile identity without freezing lease rows."""

    _require(
        after.get("userVersion") == 9
        and after.get("foreignKeyViolationCount") == 0
        and after.get("quickCheck") == ["ok"]
        and after.get("profilesIdentitySha256") == before.get("profilesIdentitySha256")
        and after.get("providers") == before.get("providers"),
        "migrated Account Center schema or protected provider profile identity failed readback",
    )


def _validate_preserved_persistence_schema(snapshot: Any) -> dict[str, Any]:
    """Require the credential-health table on P that was already upgraded before this plan."""

    try:
        return st._validate_preexisting_credential_health_schema(snapshot)
    except st.StageTransitionError as error:
        raise ForwardRecoveryError(
            "preserved Persistence image lacks the exact pre-existing credential-health schema"
        ) from error


def _implementation_preflight(
    *,
    source: dict[str, Any],
    candidate_compose: dict[str, Any],
    candidate_compose_files: list[Path],
    candidate_lock_path: Path,
    auth_path: Path,
    auth_task_id: str,
) -> dict[str, Any]:
    """Run the canonical credential-read-only preflight against the fixed candidate."""

    runtime_services = st._runtime_service_inventory(candidate_compose)
    _require(
        set(runtime_services) == set(st.OFFICE_GRAPH_RUNTIME_SERVICE_SET)
        and len(runtime_services) == MAX_RUNTIME_SERVICES,
        "candidate does not render the exact 15-service Graph bootstrap topology",
    )
    _require_no_gpu(candidate_compose)
    candidate_inputs = source["candidateInputs"]
    candidate_seed = Path(candidate_inputs["huggingfaceSeed"]["path"])
    runpod = candidate_inputs.get("runpodKey")
    notification_anchor = candidate_inputs.get("notificationAnchor")
    notification_provenance = candidate_inputs.get("notificationProvenance")
    _require(
        notification_anchor is None and notification_provenance is None,
        "Graph bootstrap must not enable credential notifications or Office authorization",
    )
    result = st.preflight.run_preflight(
        mode="connector-auth-readonly",
        compose_files=candidate_compose_files,
        env_files=[Path(row["path"]) for row in candidate_inputs["envFiles"]],
        image_lock=candidate_lock_path,
        secrets_dir=Path(source["activeSecretsDir"]),
        config_dir=Path(source["candidateConfigDir"]),
        github_hf_evidence=None,
        azure_gcp_ledger=None,
        runpod_ledger=None,
        azure_gcp_api_evidence=None,
        runpod_api_evidence=None,
        docker_prefix=source["policy"]["dockerPrefix"],
        max_memory_mib=MAX_MEMORY_MIB,
        max_cpus=MAX_CPUS,
        required_free_gib=REQUIRED_FREE_GIB,
        minimum_mem_available_mib=MINIMUM_MEM_AVAILABLE_MIB,
        connector_auth_phase=st.OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE,
        connector_auth_evidence=auth_path,
        connector_auth_task_id=auth_task_id,
        huggingface_seed_file=candidate_seed,
        candidate_runpod_key_file=(Path(runpod["path"]) if isinstance(runpod, dict) else None),
        notification_anchor_file=None,
        notification_provenance_file=None,
        ingress_exposure=source["candidatePreflight"]["ingressExposure"],
        host_caddy_binary=source["candidatePreflight"]["hostCaddyBinary"],
        openssl_binary=source["candidatePreflight"]["opensslBinary"],
    )
    _require(
        result.get("mode") == "connector-auth-readonly"
        and result.get("connectorAuthPhase") == st.OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE
        and result.get("credentialReadOnlyRuntimeReady") is True
        and result.get("fullCloudWriteDeploymentReady") is False
        and result.get("lifecycleAcceptance") == "NOT_RUN"
        and result.get("serviceCount") == MAX_RUNTIME_SERVICES
        and result.get("composeCgroupMemoryLimitMiB") == MAX_MEMORY_MIB
        and result.get("composeCgroupCpuLimit") == MAX_CPUS,
        "fresh read-only preflight did not prove the fixed 15-service resource budget",
    )
    return result


def _require_no_gpu(compose: dict[str, Any]) -> None:
    """Reject any GPU request in the closed provider-bootstrap Compose candidate."""

    services = compose.get("services")
    _require(isinstance(services, dict), "candidate Compose service map is malformed")
    for name, service in services.items():
        deploy = service.get("deploy", {}) if isinstance(service, dict) else {}
        resources = deploy.get("resources", {}) if isinstance(deploy, dict) else {}
        reservations = resources.get("reservations", {}) if isinstance(resources, dict) else {}
        if (
            not isinstance(service, dict)
            or "gpus" in service
            or service.get("runtime") == "nvidia"
            or bool(service.get("device_requests"))
            or bool(reservations.get("devices"))
        ):
            raise ForwardRecoveryError(f"Graph bootstrap candidate requests a GPU for {name}")


def _preflight_projection(
    result: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    """Retain stable read-only preflight evidence and fixed budget values."""

    planned_inputs = source["candidatePreflight"]
    host_fronted = result.get("hostFrontedIngressReadiness")
    if isinstance(host_fronted, dict):
        host_fronted = copy.deepcopy(host_fronted)
        host_fronted.pop("checkedAtUtc", None)
    return {
        "mode": result["mode"],
        "phase": result["connectorAuthPhase"],
        "ingressExposure": planned_inputs["ingressExposure"],
        "hostCaddyBinary": planned_inputs["hostCaddyBinary"],
        "opensslBinary": planned_inputs["opensslBinary"],
        "hostFrontedIngressReadiness": host_fronted,
        "credentialReadOnlyRuntimeReady": result["credentialReadOnlyRuntimeReady"],
        "fullCloudWriteDeploymentReady": result["fullCloudWriteDeploymentReady"],
        "lifecycleAcceptance": result["lifecycleAcceptance"],
        "expectedProviderScopeDigests": result["expectedProviderScopeDigests"],
        "huggingfaceRepositoryScopeDigestSha256": result[
            "huggingfaceRepositoryScopeDigestSha256"
        ],
        "notificationInputs": {
            key: value for key, value in result.items() if key.startswith("credentialNotification")
        },
        "composeCgroupMemoryLimitMiB": result["composeCgroupMemoryLimitMiB"],
        "composeCgroupCpuLimit": result["composeCgroupCpuLimit"],
        "maxMemoryMiB": MAX_MEMORY_MIB,
        "maxCpus": MAX_CPUS,
        "requiredFreeGiB": REQUIRED_FREE_GIB,
        "minimumMemAvailableMiB": MINIMUM_MEM_AVAILABLE_MIB,
    }


def _render_candidate(
    source: dict[str, Any], override_path: Path, lock_path: Path, docker: list[str]
) -> tuple[dict[str, Any], dict[str, Any], list[Path]]:
    """Render the frozen candidate plus exactly one fixed Account Center image override."""

    override = _read_private_json(override_path, "fixed Account Center Compose override")
    _validate_fixed_override(override)
    original_compose_files = [Path(row["path"]) for row in source["candidateInputs"]["composeFiles"]]
    compose_files = [*original_compose_files, override_path]
    env_files = [Path(row["path"]) for row in source["candidateInputs"]["envFiles"]]
    original_lock_path = Path(source["candidateInputs"]["imageLock"]["path"])
    old_lock = st.runtime_update._load_lock(original_lock_path)
    new_lock = st.runtime_update._load_lock(lock_path)
    _validate_candidate_lock_delta(old_lock, new_lock)
    original_compose = st.preflight._compose_config(docker, original_compose_files, env_files)
    candidate_compose = st.preflight._compose_config(docker, compose_files, env_files)
    expected_compose = copy.deepcopy(original_compose)
    expected_compose["services"]["account-center"]["image"] = FIXED_ACCOUNT_CENTER_IMAGE_REF
    _require(
        candidate_compose == expected_compose
        and candidate_compose.get("name") == source["project"],
        "fixed Account Center override changes candidate Compose beyond its image",
    )
    candidate_rows = st.runtime_update._image_rows_by_service(new_lock)
    _require(
        st._service_image(
            candidate_compose, st._lock_image_ids(new_lock), "account-center"
        )
        == {"imageRef": FIXED_ACCOUNT_CENTER_IMAGE_REF, "imageId": FIXED_ACCOUNT_CENTER_IMAGE_ID}
        and candidate_rows["account-center"]["sourceCommits"]["plugins"]
        == FIXED_PLUGINS_COMMIT,
        "rendered Account Center candidate differs from the exact fixed image lock",
    )
    _require(
        st._runtime_config_identity_for_compose(candidate_compose)
        == source["runtimeConfigIdentity"]["candidate"],
        "fixed image override changed a candidate mount or runtime configuration identity",
    )
    return candidate_compose, new_lock, compose_files


def _database_baseline(
    evidence: dict[str, Any], containers: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read only the current AC database and bind it to the frozen snapshot receipt."""

    database_path = core_recovery._database_path(containers["account-center"])
    snapshot_path = Path(evidence["snapshotPath"])
    _private_readonly_snapshot(snapshot_path, "Account Center source SQLite snapshot")
    _require(
        _sha256_file(snapshot_path) == evidence.get("snapshotSha256"),
        "Account Center source SQLite snapshot digest changed",
    )
    snapshot = core_recovery._database(snapshot_path)
    live = core_recovery._database(database_path)
    return _validate_source_database_evidence(evidence, snapshot, live, database_path), live


def _current_controls(
    source: dict[str, Any], recovery_document: dict[str, Any], containers: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture fresh accepting ingress and Navigator identities for the new operation."""

    receipt = recovery_document["coreRecovery"]
    docker = source["policy"]["dockerPrefix"]
    ingress_id = receipt["runtimeContainers"]["teams-ingress"]["id"]
    edge = st.runtime_update._current_edge_status(docker, ingress_id, timeout_seconds=15)
    _require(
        edge.get("paused") is False
        and edge.get("dead_count") == 0
        and edge.get("revision") == recovery_document.get("edge", {}).get("resumeRevision")
        and isinstance(edge.get("active_delivery_count"), int)
        and edge.get("active_delivery_count") >= 0,
        "Teams ingress is not accepting at the exact core-recovery delivery revision",
    )
    navigator_id = receipt["runtimeContainers"]["navigator"]["id"]
    nav = st.runtime_update._current_navigator_status(docker, navigator_id, timeout_seconds=15)
    expected_nav_generation = recovery_document.get("navigator", {}).get("drainGeneration")
    _require(
        nav.get("mode") == "accepting"
        and nav.get("instanceId") == recovery_document.get("navigator", {}).get("initialInstanceId")
        and nav.get("generation") == expected_nav_generation
        and containers["navigator"]["Id"] == navigator_id,
        "Navigator is not accepting in the exact core-recovery process generation",
    )
    return (
        {
            "containerId": ingress_id,
            "revision": edge["revision"],
            "deadCount": edge["dead_count"],
        },
        {
            "containerId": navigator_id,
            "instanceId": nav["instanceId"],
            "generation": nav["generation"],
        },
    )


def _capture_context(
    *,
    records: dict[str, dict[str, str]],
    auth_task_id: str,
) -> dict[str, Any]:
    """Fresh-verify every immutable input and the live mixed runtime under its project lock."""

    source_path = _verify_file_record(records["sourcePlan"], "original source plan")
    source = _read_private_json(source_path, "original source plan")
    st._validate_plan(source)
    st._verify_all_inputs(source)
    source_journal_path = _verify_file_record(records["sourceJournal"], "original source journal")
    source_journal = st._read_journal(source_journal_path)
    source_journal_sha = _sha256_file(source_journal_path)
    _validate_original_failure(source, source_journal, source_journal_sha)
    expected_journal_path = st._journal_path(
        Path(source["journalDir"]), source["operationId"]
    ).resolve(strict=True)
    _require(
        source_journal_path.resolve(strict=True) == expected_journal_path
        and Path(source_journal["planPath"]).resolve(strict=True) == source_path.resolve(strict=True),
        "original plan and full journal paths do not resolve to the same operation",
    )
    core_path = _verify_file_record(records["coreRecoveryJournal"], "core recovery journal")
    core_document = _read_private_json(core_path, "core recovery journal")
    core_receipt = _validate_core_recovery_journal(core_document, source_journal_sha)
    database_path = _verify_file_record(records["databaseEvidence"], "Account Center database evidence")
    database_evidence = _read_private_json(database_path, "Account Center database evidence")
    migration_path = _verify_file_record(records["migrationEvidence"], "Account Center migration evidence")
    migration_evidence = _read_private_json(migration_path, "Account Center migration evidence")
    _validate_migration_evidence(migration_evidence, database_evidence)
    override_path = _verify_file_record(records["fixedAcOverride"], "fixed Account Center Compose override")
    override = _read_private_json(override_path, "fixed Account Center Compose override")
    _validate_fixed_override(override)
    lock_path = _verify_file_record(records["candidateImageLock"], "candidate image lock")
    auth_path = _verify_file_record(
        records["connectorAuthEvidence"],
        "fresh connector auth evidence",
        expected_uid=10001,
    )
    _require(
        isinstance(auth_task_id, str) and 1 <= len(auth_task_id) <= 128,
        "fresh connector auth task ID is malformed",
    )

    docker = source["policy"]["dockerPrefix"]
    st.runtime_update._validate_docker_prefix(docker)
    source_lock = st.runtime_update._load_lock(
        Path(source["candidateInputs"]["imageLock"]["path"])
    )
    candidate_lock = st.runtime_update._load_lock(lock_path)
    _validate_candidate_lock_delta(source_lock, candidate_lock)
    inventory, containers = core_recovery._inventory(source["project"], docker)
    _require(
        inventory == core_receipt["runtimeContainers"],
        "live 14-service/container/config/mount inventory differs from core recovery readback",
    )
    for name, container in containers.items():
        health = container.get("State", {}).get("Health", {}).get("Status")
        _require(
            container.get("State", {}).get("Running") is True
            and health in (None, "healthy"),
            f"live {name} service is not healthy",
        )
    database_baseline, live_database = _database_baseline(database_evidence, containers)
    controls = _current_controls(source, core_document, containers)
    st._check_selection_unchanged(source)
    candidate_compose, candidate_lock, compose_files = _render_candidate(
        source, override_path, lock_path, docker
    )
    preflight_result = _implementation_preflight(
        source=source,
        candidate_compose=candidate_compose,
        candidate_compose_files=compose_files,
        candidate_lock_path=lock_path,
        auth_path=auth_path,
        auth_task_id=auth_task_id,
    )
    return {
        "source": source,
        "sourceJournal": source_journal,
        "sourceJournalSha256": source_journal_sha,
        "coreDocument": core_document,
        "coreReceipt": core_receipt,
        "inventory": inventory,
        "containers": containers,
        "databaseBaseline": database_baseline,
        "liveDatabase": live_database,
        "controls": controls,
        "candidateCompose": candidate_compose,
        "candidateLock": candidate_lock,
        "candidateComposeFiles": compose_files,
        "candidatePreflight": _preflight_projection(preflight_result, source),
    }


def _active_service_rows(source: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Project the mixed live inventory into stage-transition's service record shape."""

    rows: dict[str, Any] = {}
    for service, container in context["containers"].items():
        identity = context["inventory"][service]
        if service == st.PERSISTENCE_IMAGE_UPDATE_TARGET:
            image = source["candidateServices"][service]["candidate"]
        else:
            image = source["activeServices"][service]
            if service == "account-center":
                image = source["candidateServices"][service]["active"]
        _require(
            isinstance(image, dict) and identity["imageId"] == image["imageId"],
            f"current {service} image differs from the witnessed mixed baseline",
        )
        network_settings = container.get("NetworkSettings", {}).get("Networks", {})
        health_record = container.get("State", {}).get("Health")
        health = health_record.get("Status") if isinstance(health_record, dict) else "running"
        rows[service] = {
            "containerId": identity["id"],
            "imageRef": image["imageRef"],
            "imageId": identity["imageId"],
            "health": health,
            "networkNames": sorted(network_settings),
        }
    _require(set(rows) == set(st.RUNTIME_SERVICE_SET), "live mixed inventory is not the exact 14 services")
    return rows


def _build_execution_plan(
    *,
    operation_id: str,
    plan_sha256: str,
    plan_path: Path,
    journal_dir: Path,
    backup_dir: Path,
    records: dict[str, dict[str, str]],
    auth_task_id: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Derive the stage-helper view while preserving P and targeting only the remaining tail."""

    source = context["source"]
    execution = copy.deepcopy(source)
    execution["operationId"] = operation_id
    execution["planSha256"] = plan_sha256
    execution["targets"] = list(TARGETS)
    execution["activeServices"] = _active_service_rows(source, context)
    candidate_inputs = copy.deepcopy(source["candidateInputs"])
    candidate_inputs["composeFiles"].append(records["fixedAcOverride"])
    candidate_inputs["imageLock"] = records["candidateImageLock"]
    candidate_inputs["connectorAuthEvidence"] = records["connectorAuthEvidence"]
    execution["candidateInputs"] = candidate_inputs
    execution["candidateInputPaths"]["connectorAuthTaskId"] = auth_task_id
    execution["candidatePreflight"] = context["candidatePreflight"]
    execution["candidateServices"] = copy.deepcopy(source["candidateServices"])
    persistence = execution["candidateServices"][st.PERSISTENCE_IMAGE_UPDATE_TARGET]
    persistence["active"] = copy.deepcopy(persistence["candidate"])
    target_images = st._lock_image_ids(context["candidateLock"])
    for service in TARGETS:
        candidate_image = st._service_image(context["candidateCompose"], target_images, service)
        target_row = execution["candidateServices"][service]
        target_row["candidate"] = candidate_image
        if service != "microsoft-graph":
            active_image = target_row["active"]
            _require(
                isinstance(active_image, dict)
                and execution["activeServices"][service]["imageId"] == active_image["imageId"],
                f"current {service} image differs from its source-plan active image",
            )
    _require(
        execution["candidateServices"]["account-center"]["candidate"]
        == {"imageRef": FIXED_ACCOUNT_CENTER_IMAGE_REF, "imageId": FIXED_ACCOUNT_CENTER_IMAGE_ID},
        "continuation does not select the fixed Account Center candidate",
    )
    execution["runtimeConfigIdentity"] = copy.deepcopy(source["runtimeConfigIdentity"])
    execution["edgeDelivery"] = {
        **execution["activeServices"]["teams-ingress"],
        "revision": context["controls"][0]["revision"],
        "deadCount": context["controls"][0]["deadCount"],
    }
    execution["navigatorDrain"] = {
        **execution["activeServices"]["navigator"],
        "instanceId": context["controls"][1]["instanceId"],
        "generation": context["controls"][1]["generation"],
    }
    execution["candidateSelectionFile"] = source["candidateSelectionFile"]
    execution["selectionBackupPath"] = str(
        backup_dir / f"{operation_id}.active-selection.previous.json"
    )
    execution["journalDir"] = str(journal_dir)
    execution["database"]["backupPath"] = str(
        backup_dir / f"{operation_id}.navigator-sessions.sqlite3"
    )
    execution["candidateInputPaths"] = {
        **source["candidateInputPaths"],
        "connectorAuthTaskId": auth_task_id,
    }
    execution["policy"] = copy.deepcopy(source["policy"])
    execution["policy"].update(
        {
            "updateOrder": list(TARGETS),
            "pauseHolder": operation_id,
            "databaseRollback": "IMAGE_CONFIG_ONLY_NEVER_RESTORE_SNAPSHOT",
            "unknownDockerMutation": "RECOVERY_REQUIRED_NO_REPLAY",
            "selectionPromotion": "AFTER_ALL_HEALTH_AND_RESOURCE_READBACK",
        }
    )
    execution["exchangeReadiness"] = {"required": True, "status": "PENDING_LIVE_READBACK"}
    execution["planPath"] = str(plan_path)
    return execution


def _plan_digest(plan: dict[str, Any]) -> str:
    """Hash a wrapper and its stage-helper view without a recursive digest field."""

    unsigned = copy.deepcopy(plan)
    unsigned.pop("planSha256", None)
    execution = unsigned.get("executionPlan")
    if isinstance(execution, dict):
        execution.pop("planSha256", None)
    return _digest(unsigned)


def _seal_plan(plan: dict[str, Any]) -> str:
    plan.pop("planSha256", None)
    plan["executionPlan"].pop("planSha256", None)
    digest = _plan_digest(plan)
    plan["planSha256"] = digest
    plan["executionPlan"]["planSha256"] = digest
    return digest


def _validate_sealed_plan(plan: dict[str, Any], expected: str) -> None:
    operation_id = plan.get("operationId")
    try:
        canonical_id = str(uuid.UUID(operation_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise ForwardRecoveryError("continuation operation ID is malformed") from error
    _require(
        canonical_id == operation_id
        and plan.get("schemaVersion") == SCHEMA
        and plan.get("planSha256") == expected
        and _plan_digest(plan) == expected
        and plan.get("executionPlan", {}).get("planSha256") == expected
        and plan.get("targets") == list(TARGETS),
        "operator confirmation differs from the immutable forward-only plan",
    )


def prepare_forward_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Seal one fresh forward-only continuation without changing runtime services."""

    st.runtime_update._require_update_operator()
    output = args.output.resolve(strict=False)
    journal_dir = args.journal_dir.resolve(strict=True)
    backup_dir = args.backup_dir.resolve(strict=True)
    _private_directory(output.parent, "forward plan")
    _private_directory(journal_dir, "forward journal")
    _private_directory(backup_dir, "forward backup")
    records = {
        "sourcePlan": _file_record(args.source_plan.resolve(strict=True), "original source plan"),
        "sourceJournal": _file_record(
            args.source_journal.resolve(strict=True), "original source journal"
        ),
        "coreRecoveryJournal": _file_record(
            args.core_recovery_journal.resolve(strict=True), "core recovery journal"
        ),
        "databaseEvidence": _file_record(
            args.database_evidence.resolve(strict=True), "Account Center database evidence"
        ),
        "migrationEvidence": _file_record(
            args.migration_evidence.resolve(strict=True), "Account Center migration evidence"
        ),
        "fixedAcOverride": _file_record(
            args.fixed_ac_override.resolve(strict=True), "fixed Account Center Compose override"
        ),
        "candidateImageLock": _file_record(
            args.candidate_image_lock.resolve(strict=True), "candidate image lock"
        ),
        "connectorAuthEvidence": _file_record(
            args.connector_auth_evidence.resolve(strict=True),
            "fresh connector auth evidence",
            expected_uid=10001,
        ),
    }
    source = _read_private_json(Path(records["sourcePlan"]["path"]), "original source plan")
    with st.runtime_update._operation_lock(source["project"]):
        context = _capture_context(records=records, auth_task_id=args.connector_auth_task_id)
        operation_id = str(uuid.uuid4())
        journal_path = st._journal_path(journal_dir, operation_id)
        _require(
            not journal_path.exists() and not journal_path.is_symlink(),
            "forward journal path already exists",
        )
        execution_plan = _build_execution_plan(
            operation_id=operation_id,
            plan_sha256="0" * 64,
            plan_path=output,
            journal_dir=journal_dir,
            backup_dir=backup_dir,
            records=records,
            auth_task_id=args.connector_auth_task_id,
            context=context,
        )
        baseline = {
            "runtimeContainers": context["inventory"],
            "accountCenterDatabase": context["databaseBaseline"],
            "edge": context["controls"][0],
            "navigator": context["controls"][1],
            "selectorSha256": context["source"]["activeSelection"]["sha256"],
            "candidatePreflight": context["candidatePreflight"],
        }
        plan: dict[str, Any] = {
            "schemaVersion": SCHEMA,
            "operationId": operation_id,
            "createdAtUtc": st._utc_now(),
            "project": source["project"],
            "sourceOperationId": source["operationId"],
            "sourcePlanSha256": source["planSha256"],
            "sourceJournalSha256": context["sourceJournalSha256"],
            "coreRecoveryOperationId": CORE_RECOVERY_OPERATION_ID,
            "coreRecoveryPlanSha256": CORE_RECOVERY_PLAN_SHA256,
            "inputs": records,
            "authTaskId": args.connector_auth_task_id,
            "journalDir": str(journal_dir),
            "backupDir": str(backup_dir),
            "journalPath": str(journal_path),
            "targets": list(TARGETS),
            "baseline": baseline,
            "fixedAccountCenter": {
                "imageRef": FIXED_ACCOUNT_CENTER_IMAGE_REF,
                "imageId": FIXED_ACCOUNT_CENTER_IMAGE_ID,
                "pluginsCommit": FIXED_PLUGINS_COMMIT,
                "migrationEvidenceSha256": records["migrationEvidence"]["sha256"],
            },
            "policy": {
                "mode": "FORWARD_ONLY_NO_REPLAY",
                "maxRuntimeServices": MAX_RUNTIME_SERVICES,
                "maxMemoryMiB": MAX_MEMORY_MIB,
                "maxCpus": MAX_CPUS,
                "noMailScopesOrOfficeAuthorization": True,
                "noProviderWritesOrModelCalls": True,
                "databaseRestore": "NEVER",
            },
            "implementationSha256": _implementation_sha256(),
            "executionPlan": execution_plan,
        }
        digest = _seal_plan(plan)
        _require(
            not Path(execution_plan["database"]["backupPath"]).exists()
            and not Path(execution_plan["selectionBackupPath"]).exists(),
            "forward backup or selector archive path already exists",
        )
        _write_exclusive(output, plan)
    return {
        "state": "PREPARED_FORWARD_ONLY",
        "operationId": operation_id,
        "planPath": str(output),
        "planSha256": digest,
        "journalPath": str(journal_path),
        "targets": list(TARGETS),
    }


def _validate_plan_inputs(plan: dict[str, Any], expected: str) -> dict[str, Any]:
    """Re-read source evidence and compare fresh runtime state to the sealed plan."""

    _validate_sealed_plan(plan, expected)
    _require(
        plan.get("implementationSha256") == _implementation_sha256(),
        "forward-recovery implementation changed after planning",
    )
    context = _capture_context(records=plan["inputs"], auth_task_id=plan["authTaskId"])
    _require(
        context["source"]["operationId"] == plan["sourceOperationId"]
        and context["source"]["planSha256"] == plan["sourcePlanSha256"]
        and context["sourceJournalSha256"] == plan["sourceJournalSha256"]
        and context["coreDocument"]["operationId"] == plan["coreRecoveryOperationId"]
        and context["coreDocument"]["planSha256"] == plan["coreRecoveryPlanSha256"],
        "forward plan source operation or recovery journal changed",
    )
    expected_baseline = plan["baseline"]
    _require(
        context["inventory"] == expected_baseline["runtimeContainers"]
        and context["databaseBaseline"] == expected_baseline["accountCenterDatabase"]
        and context["controls"][0] == expected_baseline["edge"]
        and context["controls"][1] == expected_baseline["navigator"]
        and context["source"]["activeSelection"]["sha256"]
        == expected_baseline["selectorSha256"]
        and context["candidatePreflight"] == expected_baseline["candidatePreflight"],
        "live baseline or fresh provider proof changed after the plan was prepared",
    )
    return context


def _initial_ac_profile_state(context: dict[str, Any]) -> dict[str, Any]:
    live = context["liveDatabase"]
    return {
        "databasePath": context["databaseBaseline"]["databasePath"],
        "schemaSha256": live["schemaSha256"],
        "profilesIdentitySha256": live["profilesIdentitySha256"],
        "providers": live["providers"],
    }


def _read_migrated_account_center(
    container: dict[str, Any], expected_path: str, before: dict[str, Any]
) -> dict[str, Any]:
    path = core_recovery._database_path(container)
    _require(str(path) == expected_path, "migrated Account Center mounted a different database")
    after = core_recovery._database(path)
    _validate_migrated_database(before, after)
    return {
        "databasePath": str(path),
        "userVersion": after["userVersion"],
        "schemaSha256": after["schemaSha256"],
        "profilesIdentitySha256": after["profilesIdentitySha256"],
        "providers": after["providers"],
        "foreignKeyViolationCount": after["foreignKeyViolationCount"],
        "quickCheck": after["quickCheck"],
    }


def _retarget_restarted_ingress_lease(
    *,
    journal: dict[str, Any],
    journal_path: Path,
    docker: list[str],
    new_container_id: str,
) -> None:
    """Carry the exact paused CAS lease to the new ingress container after its replacement."""

    edge = journal["edge"]
    _require(
        isinstance(new_container_id, str)
        and st.FULL_CONTAINER_ID_RE.fullmatch(new_container_id) is not None
        and edge.get("pauseRevision") == edge.get("initialRevision", -1) + 1
        and edge.get("pauseHolder") == journal.get("operationId"),
        "restarted ingress lease intent is malformed",
    )
    status = st.runtime_update._current_edge_status(docker, new_container_id, timeout_seconds=15)
    _require(
        status.get("paused") is True
        and status.get("revision") == edge["pauseRevision"]
        and status.get("active_delivery_count") == 0
        and status.get("dead_count") == edge.get("initialDeadCount"),
        "restarted Teams ingress did not preserve this operation's exact paused delivery lease",
    )
    edge["previousContainerId"] = edge["containerId"]
    edge["containerId"] = new_container_id
    edge["replacedContainerId"] = new_container_id
    st._write_journal(journal_path, journal, "TEAMS_INGRESS_PAUSE_LEASE_READBACK")


def _journal_forward_recovery(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": RECOVERY_JOURNAL_SCHEMA,
        "state": "RUNNING",
        "sourceOperationId": plan["sourceOperationId"],
        "sourcePlanSha256": plan["sourcePlanSha256"],
        "sourceJournalSha256": plan["sourceJournalSha256"],
        "coreRecoveryOperationId": plan["coreRecoveryOperationId"],
        "coreRecoveryPlanSha256": plan["coreRecoveryPlanSha256"],
        "fixedAccountCenterImageId": FIXED_ACCOUNT_CENTER_IMAGE_ID,
        "mutationPolicy": "FORWARD_ONLY_NO_REPLAY_NO_DATABASE_RESTORE",
        "persistenceContainerIdPreserved": plan["baseline"]["runtimeContainers"][
            st.PERSISTENCE_IMAGE_UPDATE_TARGET
        ]["id"],
        "accountCenterDatabaseBefore": plan["baseline"]["accountCenterDatabase"],
        "accountCenterDatabaseAfterMigration": None,
    }


def apply_forward_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Apply the exact plan once, preserving P and stopping every uncertain outcome."""

    st.runtime_update._require_update_operator()
    plan_path = args.plan.resolve(strict=True)
    plan = _read_private_json(plan_path, "forward-only continuation plan")
    expected = args.confirm_plan_sha256
    _validate_sealed_plan(plan, expected)
    journal_path = Path(plan["journalPath"])
    journal_dir = Path(plan["journalDir"])
    _private_directory(journal_dir, "forward journal")
    _require(
        journal_path == st._journal_path(journal_dir, plan["operationId"])
        and not journal_path.exists()
        and not journal_path.is_symlink(),
        "forward operation journal exists; apply is never replayed",
    )
    with st.runtime_update._operation_lock(plan["project"]):
        _require(
            not journal_path.exists() and not journal_path.is_symlink(),
            "forward operation intent exists; apply is never replayed",
        )
        context = _validate_plan_inputs(plan, expected)
        execution = copy.deepcopy(plan["executionPlan"])
        docker = execution["policy"]["dockerPrefix"]
        candidate_compose, candidate_lock = st._candidate_configs(execution, docker)
        _require(
            candidate_compose == context["candidateCompose"]
            and candidate_lock == context["candidateLock"],
            "rendered fixed candidate changed after plan verification",
        )
        journal = st._make_journal(execution, plan_path)
        journal["officeGraphForwardRecovery"] = _journal_forward_recovery(plan)
        st._create_initial_journal(journal_path, journal)
        before_profiles = _initial_ac_profile_state(context)
        try:
            # ── Phase 1: Establish new delivery and executor holds ────────────
            # 第一阶段：为本次新操作建立入口暂停与 Navigator 排空租约。
            st.runtime_update._pause_edge(
                plan=execution,
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker,
            )
            st.runtime_update._drain_navigator(
                plan=execution,
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker,
            )
            snapshot = st._atomic_sqlite_snapshot(
                plan=execution,
                docker_prefix=docker,
                journal=journal,
                journal_path=journal_path,
            )
            preserved_persistence = _validate_preserved_persistence_schema(snapshot)
            journal["officeGraphForwardRecovery"]["persistenceSnapshotSha256"] = snapshot.get(
                "backupSha256"
            )
            journal["officeGraphForwardRecovery"]["persistenceSchemaSha256"] = preserved_persistence[
                "schemaSha256"
            ]
            st._write_journal(journal_path, journal, "FORWARD_PRESERVED_PERSISTENCE_VERIFIED")

            # ── Phase 2: Apply only the remaining Graph bootstrap tail ───────
            # 第二阶段：按原有顺序更新 AC、Cloud、Graph、Exchange、Teams 与 Navigator。
            for service in TARGETS:
                st._update_target(
                    plan=execution,
                    candidate_compose=candidate_compose,
                    journal=journal,
                    journal_path=journal_path,
                    docker_prefix=docker,
                    service_name=service,
                )
                if service == "account-center":
                    new_id = journal["targetProgress"][service]["healthyContainerId"]
                    inspected = st._inspect_exact_service(
                        project=execution["project"],
                        service_name=service,
                        expected_image=execution["candidateServices"][service]["candidate"],
                        has_healthcheck=True,
                        docker_prefix=docker,
                    )
                    _require(
                        inspected["containerId"] == new_id,
                        "Account Center migration container changed before database readback",
                    )
                    # Re-inspect the current candidate container so path resolution uses its mounts.
                    container_raw = json.loads(
                        st.runtime_update._checked_stdout(
                            [*docker, "inspect", new_id],
                            label="fixed Account Center migrated database mount readback",
                        )
                    )
                    _require(
                        isinstance(container_raw, list) and len(container_raw) == 1,
                        "migrated Account Center container inspection is ambiguous",
                    )
                    journal["officeGraphForwardRecovery"][
                        "accountCenterDatabaseAfterMigration"
                    ] = _read_migrated_account_center(
                        container_raw[0],
                        context["databaseBaseline"]["databasePath"],
                        before_profiles,
                    )
                    st._write_journal(journal_path, journal, "ACCOUNT_CENTER_V9_READBACK_VERIFIED")
                elif service == "exchange":
                    st._record_exchange_readiness(
                        plan=execution,
                        compose=candidate_compose,
                        journal=journal,
                        journal_path=journal_path,
                        docker_prefix=docker,
                    )
                elif service == "teams-ingress":
                    progress = journal["targetProgress"][service]
                    _retarget_restarted_ingress_lease(
                        journal=journal,
                        journal_path=journal_path,
                        docker=docker,
                        new_container_id=progress["healthyContainerId"],
                    )

            # ── Phase 3: Verify the complete candidate before selector promotion ──
            # 第三阶段：全量回读、资源门、Exchange 就绪后才提升新 selector。
            candidate_services = st._live_service_inventory(
                compose=candidate_compose,
                image_lock=candidate_lock,
                docker_prefix=docker,
                mount_identity_by_service=execution["runtimeConfigIdentity"]["candidate"],
            )
            for service, previous in execution["activeServices"].items():
                if service not in TARGETS:
                    current = candidate_services.get(service)
                    _require(
                        isinstance(current, dict)
                        and current.get("containerId") == previous["containerId"]
                        and current.get("imageId") == previous["imageId"],
                        f"untargeted {service} container/image changed during forward continuation",
                    )
            st.runtime_update._verify_live_network_membership(
                candidate_compose, candidate_services, docker
            )
            capacity = st._memory_update_actual_capacity(
                compose=candidate_compose,
                services=candidate_services,
                max_memory_mib=MAX_MEMORY_MIB,
                max_cpus=MAX_CPUS,
                docker_prefix=docker,
            )
            journal["officeGraphForwardRecovery"]["resourceLimits"] = capacity
            if journal["exchangeReadiness"].get("verified") is not True:
                st._record_exchange_readiness(
                    plan=execution,
                    compose=candidate_compose,
                    journal=journal,
                    journal_path=journal_path,
                    docker_prefix=docker,
                )
            final_ac_raw = json.loads(
                st.runtime_update._checked_stdout(
                    [*docker, "inspect", candidate_services["account-center"]["containerId"]],
                    label="final Account Center database mount readback",
                )
            )
            _require(
                isinstance(final_ac_raw, list) and len(final_ac_raw) == 1,
                "final Account Center inspection is ambiguous",
            )
            final_ac = _read_migrated_account_center(
                final_ac_raw[0],
                context["databaseBaseline"]["databasePath"],
                before_profiles,
            )
            journal["officeGraphForwardRecovery"]["accountCenterDatabaseFinal"] = final_ac
            st._resume_executor_after_transition(
                plan=execution,
                journal=journal,
                candidate_compose=candidate_compose,
                docker_prefix=docker,
            )
            journal["resourceReadback"] = st._validate_post_update_capacity(
                compose=candidate_compose,
                services=candidate_services,
                docker_prefix=docker,
                minimum_available_mib=MINIMUM_MEM_AVAILABLE_MIB,
            )
            st._write_journal(journal_path, journal, "RESOURCE_READBACK_VERIFIED")
            selection = st._promote_selection(
                plan=execution,
                journal=journal,
                journal_path=journal_path,
            )
            st._resume_ingress_after_safe_state(
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker,
            )
            delivery = st._verify_final_delivery_readback(
                journal=journal, docker_prefix=docker
            )
            journal["edge"]["latestDeadCount"] = delivery["dead_count"]
            journal["officeGraphForwardRecovery"]["state"] = FORWARD_APPLIED_STATE
            journal["officeGraphForwardRecovery"]["selector"] = st._selection_summary(selection)
            _require(
                Path(plan["inputs"]["sourcePlan"]["path"]).is_file()
                and _sha256_file(Path(plan["inputs"]["sourcePlan"]["path"]))
                == plan["inputs"]["sourcePlan"]["sha256"]
                and _sha256_file(Path(plan["inputs"]["sourceJournal"]["path"]))
                == plan["inputs"]["sourceJournal"]["sha256"],
                "original failed plan/journal changed during forward continuation",
            )
            st._write_journal(journal_path, journal, FORWARD_APPLIED_STATE, state="APPLIED")
            return {
                "state": FORWARD_APPLIED_STATE,
                "operationId": plan["operationId"],
                "planSha256": expected,
                "journalPath": str(journal_path),
                "targets": list(TARGETS),
                "preservedPersistenceContainerId": journal["officeGraphForwardRecovery"][
                    "persistenceContainerIdPreserved"
                ],
                "selector": st._selection_summary(selection),
                "deliveryPaused": delivery["paused"],
                "deadCount": delivery["dead_count"],
            }
        except Exception as error:
            journal["officeGraphForwardRecovery"]["state"] = "RECOVERY_REQUIRED"
            journal["lastErrorCode"] = "office_graph_forward_outcome_requires_readback"
            try:
                st._write_journal(
                    journal_path,
                    journal,
                    "OFFICE_GRAPH_FORWARD_RECOVERY_REQUIRED",
                    state="RECOVERY_REQUIRED",
                )
            except (OSError, st.StageTransitionError):
                # The already-created exclusive journal still blocks any apply replay.
                print(
                    "Forward recovery journal finalization failed; apply replay remains blocked.",
                    file=sys.stderr,
                )
            if isinstance(
                error,
                (
                    ForwardRecoveryError,
                    st.StageTransitionError,
                    st.runtime_update.RuntimeUpdateError,
                    st.preflight.PreflightError,
                    OSError,
                    subprocess.SubprocessError,
                ),
            ):
                raise ForwardRecoveryError(
                    "forward continuation stopped; inspect its exact journal before any further action"
                ) from error
            raise


def _add_prepare_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-plan", type=Path, required=True)
    parser.add_argument("--source-journal", type=Path, required=True)
    parser.add_argument("--core-recovery-journal", type=Path, required=True)
    parser.add_argument("--database-evidence", type=Path, required=True)
    parser.add_argument("--migration-evidence", type=Path, required=True)
    parser.add_argument("--fixed-ac-override", type=Path, required=True)
    parser.add_argument("--candidate-image-lock", type=Path, required=True)
    parser.add_argument("--connector-auth-evidence", type=Path, required=True)
    parser.add_argument("--connector-auth-task-id", required=True)
    parser.add_argument("--journal-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    """Build the narrow one-shot prepare/apply interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="seal a fresh forward-only continuation")
    _add_prepare_arguments(prepare)
    apply = commands.add_parser("apply", help="apply the exact sealed continuation once")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--confirm-plan-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one bounded forward continuation; an existing journal prevents replay."""

    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            result = prepare_forward_plan(arguments)
        else:
            result = apply_forward_plan(arguments)
    except (
        OSError,
        ForwardRecoveryError,
        st.StageTransitionError,
        st.runtime_update.RuntimeUpdateError,
        st.preflight.PreflightError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Office Graph forward recovery blocked: {error}", file=sys.stderr)
        return 3 if arguments.command == "apply" else 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
