"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_office_graph_forward_recovery.py                                 │
│  Module: tooling.deploy.teams_cloud.tests.test_office_graph_forward_recovery│
│  Role: Verify the one-shot Office Graph continuation's closed safety gates.│
│                                                                          │
│  模块职责：验证 Office Graph 一次性续跑的输入约束、P 保留和 CAS 恢复边界。│
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import copy
import importlib
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
recovery = importlib.import_module("office_graph_forward_recovery")


def _source_journal() -> tuple[dict[str, object], dict[str, object]]:
    """Build the single supported failed source operation without runtime data."""

    plan = {
        "schemaVersion": recovery.st.PLAN_SCHEMA,
        "operationId": recovery.ORIGINAL_OPERATION_ID,
        "planSha256": recovery.ORIGINAL_PLAN_SHA256,
        "phase": recovery.st.OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE,
        "targets": list(recovery.st.OFFICE_GRAPH_STAGE_TARGET_ORDER),
        "candidateServices": {
            "account-center": {
                "active": {"imageId": recovery.OLD_ACCOUNT_CENTER_IMAGE_ID}
            }
        },
    }
    progress = {
        name: {"phase": "PREPARED", "applyIntent": False, "candidateContainerId": None}
        for name in recovery.st.OFFICE_GRAPH_STAGE_TARGET_ORDER
    }
    progress[recovery.st.PERSISTENCE_IMAGE_UPDATE_TARGET] = {
        "phase": "APPLIED",
        "applyIntent": False,
        "candidateContainerId": "p" * 64,
        "healthyContainerId": "p" * 64,
    }
    progress["account-center"] = {
        "phase": "CONTAINER_READBACK",
        "applyIntent": True,
        "rollbackIntent": False,
        "candidateContainerId": "a" * 64,
    }
    journal = {
        "schemaVersion": recovery.st.JOURNAL_SCHEMA,
        "operationId": recovery.ORIGINAL_OPERATION_ID,
        "planSha256": recovery.ORIGINAL_PLAN_SHA256,
        "state": "RECOVERY_REQUIRED",
        "lastErrorCode": recovery.st.ACCOUNT_CENTER_DOWNGRADE_BLOCKED_ERROR,
        "targetProgress": progress,
        "selection": {"promoted": False, "promotionIntent": False},
    }
    return plan, journal


def _core_recovery_journal() -> dict[str, object]:
    """Build the independently restored 14-service receipt with normalized hashes."""

    containers = {
        name: {
            "id": f"{index:064x}",
            "imageId": "sha256:" + f"{index:064x}",
            "configSha256": "c" * 64,
            "mountsSha256": "b" * 64,
            "hostConfigSha256": "d" * 64,
        }
        for index, name in enumerate(sorted(recovery.st.RUNTIME_SERVICE_SET), start=1)
    }
    receipt = {
        "state": recovery.CORE_RESTORED_STATE,
        "databaseSchemaVersion": 8,
        "databaseRestorePerformed": False,
        "profileIdentitiesPreserved": True,
        "originalSelectorUnchanged": True,
        "officeEnabled": False,
        "deliveryPaused": False,
        "deadCount": 0,
        "runtimeContainers": containers,
        "retainedPersistence": containers[recovery.st.PERSISTENCE_IMAGE_UPDATE_TARGET],
        "restoredAccountCenter": {
            "containerId": containers["account-center"]["id"],
            "imageId": containers["account-center"]["imageId"],
            "health": "healthy",
        },
    }
    return {
        "schemaVersion": recovery.CORE_RECOVERY_JOURNAL_SCHEMA,
        "operationId": recovery.CORE_RECOVERY_OPERATION_ID,
        "planSha256": recovery.CORE_RECOVERY_PLAN_SHA256,
        "state": recovery.CORE_RESTORED_STATE,
        "sourceOperationId": recovery.ORIGINAL_OPERATION_ID,
        "sourcePlanSha256": recovery.ORIGINAL_PLAN_SHA256,
        "sourceJournalSha256": recovery.ORIGINAL_JOURNAL_SHA256,
        "coreRecovery": receipt,
    }


def _lock() -> dict[str, object]:
    """Build a compact valid ARM64 lock with one original Account Center row."""

    return {
        "schemaVersion": "cyrene-teams-cloud-image-lock-v1",
        "targetPlatform": "linux/arm64",
        "state": "LOCAL_ONLY",
        "taskId": "source-task",
        "createdAt": "2026-10-29T00:00:00Z",
        "planSha256": "1" * 64,
        "images": [
            {
                "service": "account-center",
                "image": "cyrene/account-center:old-candidate",
                "imageId": "sha256:" + "1" * 64,
                "platform": "linux/arm64",
            },
            {
                "service": "exchange",
                "image": "cyrene/exchange:fixed",
                "imageId": "sha256:" + "2" * 64,
                "platform": "linux/arm64",
            },
        ],
        "externalImages": {"postgres": "sha256:" + "3" * 64},
        "binaryArtifacts": [],
        "sources": {
            "plugins": {
                "remote": "https://github.com/DoHorizon-AI/Cyrene-Plugins-Official.git",
                "commit": "6d84dc802e8175226ca2d04eb73ada3b8e46ba98",
                "tree": "d" * 40,
            },
            "workspace": {
                "remote": "https://github.com/DoHorizon-AI/Cyrene-Workspace.git",
                "commit": "3" * 40,
                "tree": "4" * 40,
            },
        },
    }


def _fixed_candidate_lock() -> tuple[dict[str, object], dict[str, object]]:
    original = _lock()
    candidate = copy.deepcopy(original)
    candidate["images"][0] = {
        "service": "account-center",
        "image": recovery.FIXED_ACCOUNT_CENTER_IMAGE_REF,
        "imageId": recovery.FIXED_ACCOUNT_CENTER_IMAGE_ID,
        "platform": "linux/arm64",
        "sourceCommits": {"plugins": recovery.FIXED_PLUGINS_COMMIT},
    }
    candidate["sources"]["plugins-account-center-fix"] = {
        "remote": "https://github.com/DoHorizon-AI/Cyrene-Plugins-Official.git",
        "commit": recovery.FIXED_PLUGINS_COMMIT,
        "tree": "b02d8b39ae0b104fb08ee144b62a14fedd4da355",
    }
    candidate["taskId"] = "office-forward-ac-migration-20261030"
    candidate["createdAt"] = "2026-10-30T12:00:00Z"
    candidate["planSha256"] = "5" * 64
    return original, candidate


def test_continuation_targets_skip_applied_persistence_and_keep_canonical_tail_order() -> None:
    """The new operation begins at AC and never recreates the already healthy P image."""

    assert recovery.TARGETS == (
        "account-center",
        "cloud-management-phase2",
        "microsoft-graph",
        "exchange",
        "teams-provider",
        "teams-ingress",
        "navigator",
    )
    assert recovery.st.PERSISTENCE_IMAGE_UPDATE_TARGET not in recovery.TARGETS


def test_only_the_exact_original_failed_operation_is_eligible() -> None:
    """Reject a changed failure state, a later target mutation, or selector intent."""

    plan, journal = _source_journal()
    recovery._validate_original_failure(plan, journal, recovery.ORIGINAL_JOURNAL_SHA256)

    bad = copy.deepcopy(journal)
    bad["state"] = "APPLIED"
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_original_failure(plan, bad, recovery.ORIGINAL_JOURNAL_SHA256)

    bad = copy.deepcopy(journal)
    bad["targetProgress"]["exchange"]["applyIntent"] = True
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_original_failure(plan, bad, recovery.ORIGINAL_JOURNAL_SHA256)

    bad = copy.deepcopy(journal)
    bad["selection"]["promotionIntent"] = True
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_original_failure(plan, bad, recovery.ORIGINAL_JOURNAL_SHA256)


def test_core_recovery_receipt_binds_exact_normalized_fourteen_service_state() -> None:
    """The source recovery journal must retain P and the restored AC identity exactly."""

    document = _core_recovery_journal()
    receipt = recovery._validate_core_recovery_journal(
        document, recovery.ORIGINAL_JOURNAL_SHA256
    )
    assert set(receipt["runtimeContainers"]) == set(recovery.st.RUNTIME_SERVICE_SET)

    bad = copy.deepcopy(document)
    bad["coreRecovery"]["runtimeContainers"].pop("exchange")
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_core_recovery_journal(bad, recovery.ORIGINAL_JOURNAL_SHA256)


def test_fixed_candidate_lock_allows_only_ac_replacement_and_exact_assembly_provenance() -> None:
    """Other images/external locks stay fixed while the new AC row and receipt are pinned."""

    original, candidate = _fixed_candidate_lock()
    row = recovery._validate_candidate_lock_delta(original, candidate)
    assert row["imageId"] == recovery.FIXED_ACCOUNT_CENTER_IMAGE_ID

    bad = copy.deepcopy(candidate)
    bad["externalImages"]["postgres"] = "sha256:" + "6" * 64
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_candidate_lock_delta(original, bad)

    bad = copy.deepcopy(candidate)
    bad["sources"]["plugins-account-center-fix"]["tree"] = "7" * 40
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_candidate_lock_delta(original, bad)

    bad = copy.deepcopy(candidate)
    bad["sources"]["workspace"]["commit"] = "8" * 40
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_candidate_lock_delta(original, bad)


def test_compose_override_is_limited_to_the_fixed_account_center_image() -> None:
    valid = {"services": {"account-center": {"image": recovery.FIXED_ACCOUNT_CENTER_IMAGE_REF}}}
    recovery._validate_fixed_override(valid)

    invalid = copy.deepcopy(valid)
    invalid["services"]["exchange"] = {"image": "evil"}
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_fixed_override(invalid)


def test_preflight_projection_ignores_only_the_host_fronted_check_timestamp() -> None:
    """Repeated read-only TLS probes stay equivalent while every readiness field remains bound."""

    first = {
        "mode": "connector-auth-readonly",
        "connectorAuthPhase": recovery.st.OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE,
        "serviceCount": 16,
        "hostFrontedIngressReadiness": {
            "status": "HOST_FRONTED_STAGING_READY",
            "checkedAtUtc": "2026-10-30T12:00:00Z",
            "backendTls": "PASS",
            "publicTls": "PASS",
        },
        "credentialReadOnlyRuntimeReady": True,
        "fullCloudWriteDeploymentReady": False,
        "lifecycleAcceptance": "NOT_RUN",
        "expectedProviderScopeDigests": {"github": "a" * 64},
        "huggingfaceRepositoryScopeDigestSha256": "b" * 64,
        "composeCgroupMemoryLimitMiB": recovery.MAX_MEMORY_MIB,
        "composeCgroupCpuLimit": recovery.MAX_CPUS,
    }
    source = {
        "candidatePreflight": {
            "ingressExposure": "host-fronted-staging",
            "hostCaddyBinary": "/usr/local/bin/caddy",
            "opensslBinary": "openssl",
        },
        "runtimeConfigIdentity": {
            "candidate": {name: [] for name in recovery.st.OFFICE_GRAPH_RUNTIME_SERVICE_SET}
        },
    }
    second = copy.deepcopy(first)
    second["hostFrontedIngressReadiness"]["checkedAtUtc"] = "2026-10-30T12:05:00Z"
    assert recovery._preflight_projection(first, source) == recovery._preflight_projection(
        second, source
    )

    second["hostFrontedIngressReadiness"]["publicTls"] = "FAIL"
    assert recovery._preflight_projection(first, source) != recovery._preflight_projection(
        second, source
    )

    projected = recovery._preflight_projection(first, source)
    assert projected["declaredComposeServiceCount"] == 16
    assert projected["runtimeServiceCount"] == 15


def test_candidate_preflight_accepts_one_declared_operator_outside_runtime_budget() -> None:
    """The temporary directory operator is declared in Compose but is not a runtime service."""

    runtime_names = set(recovery.st.OFFICE_GRAPH_RUNTIME_SERVICE_SET)
    candidate_compose = {
        "services": {**{name: {} for name in runtime_names}, "directory-operator": {}}
    }
    source = {
        "candidateInputs": {
            "huggingfaceSeed": {"path": "/private/hf-seed"},
            "runpodKey": None,
            "envFiles": [],
            "notificationAnchor": None,
            "notificationProvenance": None,
        },
        "activeSecretsDir": "/private/secrets",
        "candidateConfigDir": "/private/config",
        "policy": {"dockerPrefix": ["docker"]},
        "candidatePreflight": {
            "ingressExposure": "host-fronted-staging",
            "hostCaddyBinary": "/usr/local/bin/caddy",
            "opensslBinary": "openssl",
        },
    }
    preflight_result = {
        "mode": "connector-auth-readonly",
        "connectorAuthPhase": recovery.st.OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE,
        "credentialReadOnlyRuntimeReady": True,
        "fullCloudWriteDeploymentReady": False,
        "lifecycleAcceptance": "NOT_RUN",
        "composeCgroupMemoryLimitMiB": recovery.MAX_MEMORY_MIB,
        "composeCgroupCpuLimit": recovery.MAX_CPUS,
        "serviceCount": 16,
    }
    with (
        mock.patch.object(
            recovery.st,
            "_runtime_service_inventory",
            return_value={name: {} for name in runtime_names},
        ),
        mock.patch.object(
            recovery.st.preflight, "run_preflight", return_value=preflight_result
        ),
    ):
        result = recovery._implementation_preflight(
            source=source,
            candidate_compose=candidate_compose,
            candidate_compose_files=[Path("/private/candidate.json")],
            candidate_lock_path=Path("/private/image-lock.json"),
            auth_path=Path("/private/auth.json"),
            auth_task_id="office-forward-auth-20261030",
        )
    assert result["serviceCount"] == 16

    invalid = {**preflight_result, "serviceCount": 15}
    with (
        mock.patch.object(
            recovery.st,
            "_runtime_service_inventory",
            return_value={name: {} for name in runtime_names},
        ),
        mock.patch.object(recovery.st.preflight, "run_preflight", return_value=invalid),
        pytest.raises(recovery.ForwardRecoveryError),
    ):
        recovery._implementation_preflight(
            source=source,
            candidate_compose=candidate_compose,
            candidate_compose_files=[Path("/private/candidate.json")],
            candidate_lock_path=Path("/private/image-lock.json"),
            auth_path=Path("/private/auth.json"),
            auth_task_id="office-forward-auth-20261030",
        )


def test_fixed_migration_proof_requires_an_isolated_repeated_v8_to_v9_pass() -> None:
    database = {"snapshotSha256": "8" * 64}
    evidence = {
        "state": "FIXED_ACCOUNT_CENTER_REAL_SNAPSHOT_MIGRATION_PASS",
        "deployed": False,
        "activeContainerIdsUnchanged": True,
        "liveDatabaseMounted": False,
        "secretsMounted": False,
        "network": "none",
        "platform": "linux/arm64",
        "imageId": recovery.FIXED_ACCOUNT_CENTER_IMAGE_ID,
        "pluginsCommit": recovery.FIXED_PLUGINS_COMMIT,
        "snapshotSha256": "8" * 64,
        "imageArchiveSha256": "9" * 64,
        "migration": {
            "fromVersion": 8,
            "toVersion": 9,
            "allPreexistingColumnsAndRowsPreserved": True,
            "foreignKeyViolationCount": 0,
            "quickCheck": ["ok"],
            "secondInitializationPassed": True,
        },
    }
    recovery._validate_migration_evidence(evidence, database)

    bad = copy.deepcopy(evidence)
    bad["liveDatabaseMounted"] = True
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_migration_evidence(bad, database)

    bad = copy.deepcopy(evidence)
    bad["migration"]["secondInitializationPassed"] = False
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_migration_evidence(bad, database)


def test_forward_source_database_receipt_allows_lease_churn_but_binds_profile_identity() -> None:
    """Accept the dedicated forward snapshot schema without comparing mutable live table rows."""

    database_path = Path("/var/lib/cyrene/account-center/accounts.sqlite3")
    snapshot = {
        "userVersion": 8,
        "schemaSha256": "a" * 64,
        "tables": {"provider_credential_leases": {"rowsDigest": "b" * 64}},
        "providers": [{"provider": "github", "count": 1}],
        "profilesIdentitySha256": "c" * 64,
        "foreignKeyViolationCount": 0,
        "quickCheck": ["ok"],
    }
    live = copy.deepcopy(snapshot)
    live["tables"]["provider_credential_leases"]["rowsDigest"] = "d" * 64
    evidence = {
        "state": "REAL_READONLY_ACCOUNT_CENTER_FORWARD_SNAPSHOT",
        "liveDatabaseMutated": False,
        "databasePath": str(database_path),
        "userVersion": 8,
        "schemaSha256": "a" * 64,
        "tables": snapshot["tables"],
        "providers": snapshot["providers"],
        "profilesIdentitySha256": "c" * 64,
        "foreignKeyViolationCount": 0,
        "quickCheck": ["ok"],
        "snapshotPath": "/private/account-center-before-forward.sqlite3",
        "snapshotSha256": "e" * 64,
    }
    baseline = recovery._validate_source_database_evidence(
        evidence, snapshot, live, database_path
    )
    assert baseline["profilesIdentitySha256"] == "c" * 64

    changed_profile = copy.deepcopy(live)
    changed_profile["profilesIdentitySha256"] = "f" * 64
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_source_database_evidence(
            evidence, snapshot, changed_profile, database_path
        )


def test_private_snapshot_accepts_only_readonly_mode_0400_artifacts(tmp_path: Path) -> None:
    """Keep the supplied SQLite snapshot immutable while hashing and querying it read-only."""

    artifact_dir = tmp_path / ".artifacts"
    artifact_dir.mkdir(mode=0o700)
    snapshot = artifact_dir / "snapshot.sqlite3"
    snapshot.write_bytes(b"sqlite snapshot fixture")
    snapshot.chmod(0o400)
    recovery._private_readonly_snapshot(snapshot, "fixture snapshot")

    snapshot.chmod(0o600)
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._private_readonly_snapshot(snapshot, "fixture snapshot")

def test_final_database_gate_allows_lease_row_churn_but_protects_schema_and_profiles() -> None:
    """Long-lived credential lease rows can change without weakening owner/scope checks."""

    before = {
        "userVersion": 8,
        "profilesIdentitySha256": "a" * 64,
        "providers": [{"provider": "github", "count": 1}],
        "tables": {"provider_credential_leases": {"rowsDigest": "b" * 64}},
    }
    after = {
        "userVersion": 9,
        "foreignKeyViolationCount": 0,
        "quickCheck": ["ok"],
        "profilesIdentitySha256": "a" * 64,
        "providers": [{"provider": "github", "count": 1}],
        "tables": {"provider_credential_leases": {"rowsDigest": "c" * 64}},
    }
    recovery._validate_migrated_database(before, after)

    bad = copy.deepcopy(after)
    bad["profilesIdentitySha256"] = "d" * 64
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_migrated_database(before, bad)

    bad = copy.deepcopy(after)
    bad["foreignKeyViolationCount"] = 1
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_migrated_database(before, bad)


def test_graph_continuation_uses_the_explicit_preexisting_persistence_schema_gate() -> None:
    """The standard phase helper is a no-op for Graph bootstrap, so use its exact schema gate."""

    snapshot = {"schemaSha256": "a" * 64}
    with mock.patch.object(
        recovery.st,
        "_validate_preexisting_credential_health_schema",
        return_value=snapshot,
    ) as validate:
        assert recovery._validate_preserved_persistence_schema(snapshot) is snapshot
    validate.assert_called_once_with(snapshot)


def test_restarted_ingress_must_carry_exact_paused_revision_before_lease_retarget() -> None:
    """Switch the journal edge ID only after the replacement proves the held CAS revision."""

    journal = {
        "operationId": "4de6a2a7-3db2-4313-b781-7c704e3e1f01",
        "edge": {
            "containerId": "a" * 64,
            "initialRevision": 12,
            "pauseRevision": 13,
            "pauseHolder": "4de6a2a7-3db2-4313-b781-7c704e3e1f01",
            "initialDeadCount": 0,
        },
    }
    status = {
        "paused": True,
        "revision": 13,
        "active_delivery_count": 0,
        "dead_count": 0,
    }
    with (
        mock.patch.object(recovery.st.runtime_update, "_current_edge_status", return_value=status),
        mock.patch.object(recovery.st, "_write_journal"),
    ):
        recovery._retarget_restarted_ingress_lease(
            journal=journal,
            journal_path=Path("/private/op.json"),
            docker=["docker"],
            new_container_id="b" * 64,
        )
    assert journal["edge"]["containerId"] == "b" * 64
    assert journal["edge"]["previousContainerId"] == "a" * 64

    bad_journal = copy.deepcopy(journal)
    bad_journal["edge"]["containerId"] = "a" * 64
    with (
        mock.patch.object(
            recovery.st.runtime_update,
            "_current_edge_status",
            return_value={**status, "revision": 14},
        ),
        mock.patch.object(recovery.st, "_write_journal"),
        pytest.raises(recovery.ForwardRecoveryError),
    ):
        recovery._retarget_restarted_ingress_lease(
            journal=bad_journal,
            journal_path=Path("/private/op.json"),
            docker=["docker"],
            new_container_id="b" * 64,
        )
