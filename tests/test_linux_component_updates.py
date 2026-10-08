"""Focused Linux component updater protocol and runtime integration tests."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
UPDATES_PATH = WORKSPACE_ROOT / "packaging" / "component_updates.py"
HELPER_PATH = WORKSPACE_ROOT / "packaging" / "cyrene-component-update-helper"
POLICY_PATH = WORKSPACE_ROOT / "packaging" / "org.cyrene.component-update.policy"
DEB_BUILDER_PATH = WORKSPACE_ROOT / "packaging" / "build-deb.sh"
spec = importlib.util.spec_from_file_location("cyrene_component_updates_test", UPDATES_PATH)
assert spec is not None and spec.loader is not None
updates = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = updates
spec.loader.exec_module(updates)


BROKER_BEGIN_NO_TOKEN_RESULTS = {
    "ACTIVE_TASKS": {
        "status": "ACTIVE_TASKS",
        "maintenance_token": None,
        "gate_generation": 7,
        "blocker_codes": ["ACTIVE_TASKS_PRESENT"],
    },
    "UNKNOWN": {
        "status": "UNKNOWN",
        "maintenance_token": None,
        "gate_generation": 7,
        "blocker_codes": ["ACTIVITY_SOURCE_UNKNOWN"],
    },
    "IDLE_RUNTIME_REQUIRES_UNLOAD": {
        "status": "IDLE_RUNTIME_REQUIRES_UNLOAD",
        "maintenance_token": None,
        "gate_generation": 7,
        "blocker_codes": ["IDLE_RUNTIME_REQUIRES_UNLOAD"],
    },
    "MAINTENANCE_ACTIVE": {
        "status": "MAINTENANCE_ACTIVE",
        "maintenance_token": None,
        "gate_generation": 7,
        "blocker_codes": ["MAINTENANCE_ALREADY_ACTIVE"],
    },
    # Sanitized BeginMaintenanceResult serialized by the real broker when the
    # expected gate generation changes between readiness and Begin.
    "STALE_READINESS": {
        "status": "STALE_READINESS",
        "maintenance_token": None,
        "gate_generation": 101,
        "blocker_codes": ["READINESS_GENERATION_STALE"],
    },
    "USER_CONFIRMATION_REQUIRED": {
        "status": "USER_CONFIRMATION_REQUIRED",
        "maintenance_token": None,
        "gate_generation": 7,
        "blocker_codes": ["USER_CONFIRMATION_REQUIRED"],
    },
}
BROKER_READINESS_STATUS_WIRE_VALUES = frozenset(
    {
        "READY",
        "ACTIVE_TASKS",
        "UNKNOWN",
        "IDLE_RUNTIME_REQUIRES_UNLOAD",
        "MAINTENANCE_ACTIVE",
        "STALE_READINESS",
        "USER_CONFIRMATION_REQUIRED",
    }
)


def _empty_updater(tmp_path: Path) -> updates.ComponentUpdater:
    catalog_path = tmp_path / "component-catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "generation": 1,
                "defaultChannel": "stable",
                "channels": {"stable": {}, "preview": {}},
                "components": [],
                "targets": [],
                "publishers": [],
            }
        ),
        encoding="utf-8",
    )
    return updates.ComponentUpdater(
        catalog_path=catalog_path,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "update-state",
        install_root=tmp_path / "install",
        broker_path=tmp_path / "missing-broker",
        trusted_catalog_digest=None,
    )


def test_verified_release_index_separates_manifest_bytes_from_archive_attestation() -> None:
    fixture_root = (
        WORKSPACE_ROOT
        / "tests"
        / "fixtures"
        / "workload-attestation-subjects"
        / "catalyst-4ad950"
    )
    index_bytes = (fixture_root / "component-release-index-v1.json").read_bytes()
    manifest_bytes = (fixture_root / "catalyst-ubuntu24-manifest.json").read_bytes()
    index = json.loads(index_bytes)
    manifest = json.loads(manifest_bytes)
    artifact = manifest["artifact"]
    component = next(
        row
        for row in json.loads(
            (WORKSPACE_ROOT / "governance" / "component-catalog-v2.json").read_text(
                encoding="utf-8"
            )
        )["components"]
        if row["componentId"] == manifest["componentId"]
    )
    artifact_digest = artifact["sha256"]
    manifest_asset_digest = "sha256:" + updates.hashlib.sha256(manifest_bytes).hexdigest()
    index_asset_digest = "sha256:" + updates.hashlib.sha256(index_bytes).hexdigest()
    candidate = updates.Candidate(
        component=component,
        manifest=manifest,
        manifest_digest=manifest["manifestDigest"],
        artifact_digest=artifact_digest,
        manifest_uri=next(
            row["manifestUri"]
            for row in index["releases"]
            if row["componentId"] == manifest["componentId"]
            and row["manifestDigest"] == manifest["manifestDigest"]
        ),
        index=index,
        index_uri=(
            "https://github.com/DoHorizon-AI/Cyrene-Catalyst/releases/download/"
            f"{manifest['releaseId']}/component-release-index-v1.json"
        ),
        release_tag=manifest["releaseId"],
        index_asset_name="component-release-index-v1.json",
        index_asset_digest=index_asset_digest,
        manifest_asset_digest=manifest_asset_digest,
    )
    updater = SimpleNamespace(
        _publisher_for_component=lambda _component: {
            "repository": "DoHorizon-AI/Cyrene-Catalyst",
            "workflow": "DoHorizon-AI/Cyrene-Catalyst/.github/workflows/component-release.yml",
        }
    )

    trusted = updates.ComponentUpdater._trusted_release_indexes(updater, [candidate])
    release = trusted["indexes"][0]["manifests"][0]

    assert release["manifestAssetDigest"] == manifest_asset_digest
    assert release["manifestDigest"] == manifest["manifestDigest"]
    assert release["attestationRef"]["subjectName"] == artifact["uri"].rsplit("/", 1)[-1]
    assert release["attestationRef"]["subjectDigest"] == artifact_digest
    assert "manifestAssetAttestationRef" not in release
    assert release["attestationRef"]["subjectDigest"] not in {
        release["manifestAssetDigest"],
        release["manifestDigest"],
    }

    v2_candidate = updates.Candidate(
        **{
            **candidate.__dict__,
            "manifest": {**manifest, "schemaVersion": 2},
        }
    )
    v2_trusted = updates.ComponentUpdater._trusted_release_indexes(updater, [v2_candidate])
    v2_release = v2_trusted["indexes"][0]["manifests"][0]
    assert v2_release["manifestAssetAttestationRef"]["subjectName"] == (
        candidate.manifest_uri.rsplit("/", 1)[-1]
    )
    assert v2_release["manifestAssetAttestationRef"]["subjectDigest"] == manifest_asset_digest


def test_workload_catalyst_token_projection_never_journals_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _empty_updater(tmp_path)
    token = "t" * 64
    group_id = 1234
    monkeypatch.setattr(
        updater,
        "_workload_catalyst_token_configuration",
        lambda *, generate: (token, updates.DEFAULT_CATALYST_API_TOKEN, group_id),
    )
    monkeypatch.setattr(updater, "_ensure_workload_config_directory", lambda *args, **kwargs: None)
    monkeypatch.setattr(updater, "_daemon_reload", lambda: None)
    written: dict[str, bytes] = {}

    def write_config(transaction, _transaction_path, *, path, content, group_id, mode, entry_kind):
        written[str(path)] = content
        digest = "sha256:" + updates.hashlib.sha256(content).hexdigest()
        if entry_kind != "catalyst-api-token":
            transaction.setdefault("managedConfigFiles", []).append(
                {
                    "path": str(path),
                    "kind": entry_kind,
                    "priorDigest": None,
                    "writtenDigest": digest,
                    "mode": mode,
                    "groupId": group_id,
                }
            )
        return digest

    monkeypatch.setattr(updater, "_write_workload_managed_config", write_config)
    transaction: dict[str, object] = {}
    journal_path = tmp_path / "transaction.json"
    identity = updater._project_workload_catalyst_auth(transaction, journal_path)

    assert written[str(updates.DEFAULT_CATALYST_API_TOKEN)] == f"{token}\n".encode()
    assert written[str(updates.DEFAULT_CATALYST_AUTH_ENVIRONMENT)] == (
        f"CYRENE_DATA_TOOLS_TOKEN={token}\n".encode()
    )
    assert (
        written[str(updates.DEFAULT_STUDIO_CONTROL_ENVIRONMENT)]
        == (
            "STUDIO_CATALYST_URL=http://127.0.0.1:8004\n"
            f"STUDIO_CATALYST_API_TOKEN_FILE={updates.DEFAULT_CATALYST_API_TOKEN}\n"
        ).encode()
    )
    assert transaction["catalystAuth"] == identity
    assert all(
        item["kind"] != "catalyst-api-token" for item in transaction.get("managedConfigFiles", [])
    )
    assert token not in json.dumps(transaction)
    assert token not in journal_path.read_text(encoding="utf-8")


def test_workload_catalyst_auth_reuses_protected_service_owned_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _empty_updater(tmp_path)
    group_id = 1234
    user_id = 5678
    token = "a" * 64
    files = {
        updates.DEFAULT_CATALYST_API_TOKEN: (token + "\n").encode(),
        updates.DEFAULT_CATALYST_AUTH_ENVIRONMENT: f"CYRENE_DATA_TOOLS_TOKEN={token}\n".encode(),
        updates.DEFAULT_STUDIO_CONTROL_ENVIRONMENT: (
            "STUDIO_CATALYST_URL=http://127.0.0.1:8004\n"
            f"STUDIO_CATALYST_API_TOKEN_FILE={updates.DEFAULT_CATALYST_API_TOKEN}\n"
        ).encode(),
    }
    monkeypatch.setattr(updates.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=group_id))
    monkeypatch.setattr(updates.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=user_id))
    monkeypatch.setattr(updater, "_check_workload_catalyst_auth_conflicts", lambda _gid: None)
    monkeypatch.setattr(
        updater,
        "_read_workload_protected_file",
        lambda path, **_kwargs: files.get(path),
    )
    monkeypatch.delenv("CYRENE_DATA_TOOLS_TOKEN", raising=False)

    assert updater._workload_catalyst_token_configuration(generate=False) == (
        token,
        updates.DEFAULT_CATALYST_API_TOKEN,
        group_id,
    )


def test_workload_managed_token_writer_accepts_exact_cyrene_0600_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _empty_updater(tmp_path)
    token = "b" * 64
    group_id = 1234
    user_id = 5678
    token_path = updates.DEFAULT_CATALYST_API_TOKEN
    allowed: set[tuple[int, int, int]] = set()

    def read_protected(_path, *, allowed_identities, **_kwargs):
        allowed.update(allowed_identities)
        return f"{token}\n".encode()

    monkeypatch.setattr(updates.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=user_id))
    monkeypatch.setattr(updater, "_read_workload_protected_file", read_protected)
    transaction: dict[str, object] = {}
    digest = updater._write_workload_managed_config(
        transaction,
        tmp_path / "transaction.json",
        path=token_path,
        content=f"{token}\n".encode(),
        group_id=group_id,
        mode=0o640,
        entry_kind="catalyst-api-token",
    )

    assert digest == "sha256:" + updates.hashlib.sha256(f"{token}\n".encode()).hexdigest()
    assert (user_id, group_id, 0o600) in allowed
    assert transaction.get("managedConfigFiles", []) == []


def test_workload_auth_preflight_rejects_token_in_general_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _empty_updater(tmp_path)
    token = "c" * 64

    def read_protected(path, **_kwargs):
        if path == Path("/etc/cyrene/cyrene.env"):
            return f"CYRENE_DATA_TOOLS_TOKEN={token}\n".encode()
        return None

    monkeypatch.setattr(updater, "_read_workload_protected_file", read_protected)

    with pytest.raises(updates.UpdateError) as error:
        updater._check_workload_catalyst_auth_conflicts(1234)

    assert error.value.code == "CATALYST_AUTH_CONFIGURATION_CONFLICT"


def test_workload_package_daemon_stop_requires_fresh_owner_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _empty_updater(tmp_path)
    monkeypatch.setattr(updater, "_package_product_unit_state", lambda _unit: "active")

    class Resolver:
        @staticmethod
        def potential_component_ids(_catalog, _workload_id):
            return ("cyrene-tools-dataset-preparation",)

    monkeypatch.setattr(updater, "_load_workload_resolver", lambda: Resolver())
    monkeypatch.setattr(
        updater,
        "_read_workload_package_inventory",
        lambda _workload, _components: {
            "sourceBindings": [
                {
                    "sourceId": "cyrene-plugin-standalone-operator",
                    "bindingId": "cyrene-plugin-owner-dataset-preparation",
                    "state": "RUNNING",
                }
            ]
        },
    )

    with pytest.raises(updates.UpdateError) as error:
        updater._workload_inventory_before_daemon_stop("plugins")

    assert error.value.code == "COMPONENT_IN_USE"


def _sdk_identity(
    component: dict[str, object], *, source_plan_id: str = "plan-" + "a" * 32
) -> dict[str, object]:
    index_identity = component["indexIdentity"]
    return {
        **component,
        "artifactDigest": component["digest"],
        "releaseIdentity": component["manifestDigest"],
        "releaseTag": index_identity["releaseTag"],
        "installed": True,
        "active": True,
        "verification": {"identityAttested": True},
        "identityAttested": True,
        "pythonPath": "/opt/cyrene/workload-operator/releases/test/venv/bin/python",
        "receiptPath": "/opt/cyrene/workload-operator/releases/test/.workload-sdk-install.json",
        "sourceIdentity": {
            "artifactDigest": component["digest"],
            "manifestDigest": component["manifestDigest"],
            "manifestAssetDigest": component["manifestAssetDigest"],
            "releaseId": component["releaseId"],
            "planId": source_plan_id,
            "planDigest": "sha256:" + "f" * 64,
        },
    }


def _sdk_selected_identity(*, version: str = "0.1.0") -> dict[str, object]:
    manifest_digest = "sha256:" + ("a" if version == "0.1.0" else "b") * 64
    asset_digest = "sha256:" + ("c" if version == "0.1.0" else "d") * 64
    artifact_digest = "sha256:" + ("e" if version == "0.1.0" else "f") * 64
    release_tag = f"preview-cyrene-runtime-maintenance-sdk-{version}-" + "1" * 40
    publisher = {
        "id": "official-platform-runtime-maintenance-sdk",
        "repository": "DoHorizon-AI/Cyrene-Platform",
        "workflow": "DoHorizon-AI/Cyrene-Platform/.github/workflows/runtime-maintenance-sdk-release.yml",
        "tagFormat": "component-source-sha",
    }
    return {
        "componentId": "cyrene-runtime-maintenance-sdk",
        "artifactKind": "python-bundle",
        "version": version,
        "targetId": "linux-ubuntu-24.04-x86_64-python-3.12-library",
        "releaseId": "preview-cyrene-runtime-maintenance-sdk-" + version + "-" + "1" * 40,
        "manifestUri": "https://example.invalid/component-release-manifest-v2.json",
        "manifestDigest": manifest_digest,
        "manifestAssetDigest": asset_digest,
        "digest": artifact_digest,
        "indexIdentity": {
            "assetName": "component-release-index-v1.json",
            "assetUri": "https://example.invalid/component-release-index-v1.json",
            "assetDigest": "sha256:" + "2" * 64,
            "indexDigest": "sha256:" + "3" * 64,
            "releaseTag": release_tag,
            "publisherIdentity": publisher,
        },
        "publisherIdentity": publisher,
        "attestationRef": {
            "repository": publisher["repository"],
            "workflow": publisher["workflow"],
            "sourceCommit": "1" * 40,
            "subjectName": "component-release-manifest-v2.json",
            "subjectDigest": asset_digest,
        },
    }


def test_workload_sdk_prepare_journals_prior_before_activation_and_resumes_readback(
    tmp_path: Path,
) -> None:
    updater = _empty_updater(tmp_path)
    plan_id = "plan-" + "a" * 32
    plan_digest = "sha256:" + "f" * 64
    selected = _sdk_selected_identity()
    staged_identity = {
        "archivePath": "/var/lib/cyrene-updates/plans/test/sdk.tar.gz",
        "bundlePath": "/var/lib/cyrene-updates/plans/test/sdk-bundle",
        "wheelPath": "/var/lib/cyrene-updates/plans/test/sdk-bundle/cyrene_runtime_maintenance-0.1.0-py3-none-any.whl",
        "wheelDigest": "sha256:" + "9" * 64,
        "planId": plan_id,
        "planDigest": plan_digest,
    }
    staged = {"status": "staged", "stagedIdentity": staged_identity}
    prior = _sdk_identity(
        _sdk_selected_identity(version="0.0.9"), source_plan_id="plan-" + "0" * 32
    )
    prepared = _sdk_identity(selected)
    transaction_path = updater._private_state_directory("transactions") / f"{plan_id}.json"
    transaction = {"planId": plan_id, "planDigest": plan_digest, "phase": "applying"}
    updates._atomic_json(transaction_path, transaction)

    class SdkModule:
        current = prior
        calls = 0

        @classmethod
        def read_workload_sdk_environment(cls):
            return cls.current

        @classmethod
        def prepare_workload_sdk_environment(cls, _component, _staged):
            journal = json.loads(transaction_path.read_text(encoding="utf-8"))
            assert journal["sdkPrepareIntent"]["priorIdentity"] == prior
            assert journal["sdkPrepareIntent"]["status"] == "pending"
            cls.calls += 1
            cls.current = prepared
            raise RuntimeError("simulated process failure after current pointer switch")

        @classmethod
        def restore_workload_sdk_environment(cls, prior_identity, *, expected_current):
            assert prior_identity == prior
            assert expected_current == cls.current == prepared
            cls.current = prior
            return prior

    updater._load_workload_sdk_environment = lambda: SdkModule

    with pytest.raises(updates.UpdateError) as error:
        updater._prepare_workload_sdk_durably(
            transaction,
            transaction_path,
            SdkModule,
            selected,
            staged,
            plan_id=plan_id,
            plan_digest=plan_digest,
        )
    assert error.value.code == "WORKLOAD_SDK_INSTALL_FAILED"
    assert SdkModule.calls == 1

    recovered = updater._prepare_workload_sdk_durably(
        transaction,
        transaction_path,
        SdkModule,
        selected,
        staged,
        plan_id=plan_id,
        plan_digest=plan_digest,
    )
    assert recovered == prepared
    assert SdkModule.calls == 1
    journal = json.loads(transaction_path.read_text(encoding="utf-8"))
    assert journal["sdkPrepareIntent"]["status"] == "prepared"
    assert journal["sdkEnvironment"] == prepared

    updater._restore_workload_sdk_environment(transaction, transaction_path)
    assert SdkModule.current == prior
    journal = json.loads(transaction_path.read_text(encoding="utf-8"))
    assert journal["sdkPrepareIntent"]["status"] == "restored"
    assert journal["sdkEnvironmentRestored"] == prior


def test_source_update_intent_reconcile_returns_committed_result_without_reprojection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _empty_updater(tmp_path)
    plan_id = "plan-" + "a" * 32
    plan_digest = "sha256:" + "b" * 64
    phase = "package-only"
    result = {
        "catalogGeneration": 6,
        "catalogDigest": "sha256:" + "c" * 64,
        "policyGeneration": 6,
        "policyDigest": "sha256:" + "d" * 64,
        "sourceIdentities": [],
        "bindings": [],
    }
    source_identity = {
        "cyrene-catalyst": {"uid": 12001, "gid": 12002, "source_token_sha256": "e" * 64}
    }
    intent = {
        "schemaVersion": 1,
        "phase": phase,
        "requestId": "cyrene-wsource-package-only-" + "a" * 32,
        "previousCatalogGeneration": 5,
        "expectedCatalogGeneration": 6,
        "previousPolicyDigest": "sha256:" + "f" * 64,
        "bindingScopes": {"cyrene-catalyst": []},
        "sourceIdentity": source_identity,
        "sourceArguments": ["--source", "cyrene-catalyst=12001:12002"],
        "runtimeBindings": {"cyrene-catalyst": []},
        "changed": True,
        "selectedBindings": [],
        "componentArtifactDigests": {"cyrene-tools-dataset-preparation": "sha256:" + "9" * 64},
        "requiredHoldComponentIds": ["cyrene-tools-dataset-preparation"],
        "parentPlanId": plan_id,
        "parentPlanDigest": plan_digest,
    }
    transaction = {
        "planId": plan_id,
        "planDigest": plan_digest,
        "maintenanceToken": "held-token-private",
        "sourceUpdateIntent": intent,
        "sourceUpdateIntents": {phase: intent},
    }
    transaction_path = updater._private_state_directory("transactions") / f"{plan_id}.json"
    updates._atomic_json(transaction_path, transaction)

    captured: dict[str, object] = {}

    class SourceUpdate:
        def __init__(self, **fields: object) -> None:
            captured["update"] = fields

    class RuntimeHelper:
        WorkloadSourceUpdate = SourceUpdate

        @staticmethod
        def reconcile_workload_source_update(
            update: SourceUpdate, **kwargs: object
        ) -> dict[str, object]:
            captured["reconcile"] = kwargs
            assert captured["update"]["expected_generation"] == 6
            return result

    monkeypatch.setattr(updater, "_load_workload_package_runtime", lambda: RuntimeHelper)
    monkeypatch.setattr(
        updater, "_workload_hold_echo", lambda _transaction: {"transaction_id": "held"}
    )
    generation_writes: list[int] = []
    monkeypatch.setattr(
        updater,
        "_write_workload_activity_generation",
        lambda generation, **_kwargs: generation_writes.append(generation),
    )

    recovered = updater._update_workload_source_policy(
        transaction,
        transaction_path,
        source_policy={},
        selected_rows=[],
        installation_records={},
        phase=phase,
    )

    assert recovered == result
    assert captured["reconcile"]["activity_catalog_path"] == updater.activity_catalog_path
    assert generation_writes == [6]
    assert transaction["sourceUpdateIntents"][phase]["committed"] is True
    assert transaction["sourceUpdateIntents"][phase]["result"] == result
    assert transaction["sourceUpdates"][phase] == result


def test_legacy_source_update_intent_persists_success_history_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _empty_updater(tmp_path)
    plan_id = "plan-" + "a" * 32
    plan_digest = "sha256:" + "b" * 64
    phase = "core-runtime"
    component_id = "cyrene-client-workspace-control"
    digest = "sha256:" + "c" * 64
    prior_policy_digest = "sha256:" + "d" * 64
    source_identity = {
        "cyrene-catalyst": {
            "uid": 12001,
            "gid": 12002,
            "source_token_sha256": "e" * 64,
        }
    }
    binding_scopes = {"cyrene-catalyst": []}
    runtime_bindings = {"cyrene-catalyst": []}
    source_arguments = ("--source", "cyrene-catalyst=12001:12002")
    selected_bindings: tuple[dict[str, str], ...] = ()
    component_artifact_digests = {component_id: digest}
    required_hold_component_ids = (component_id,)
    update = SimpleNamespace(
        source_arguments=source_arguments,
        binding_scopes=binding_scopes,
        source_identity=source_identity,
        runtime_bindings=runtime_bindings,
        expected_generation=6,
        changed=True,
        previous_policy_digest=prior_policy_digest,
        selected_binding_ids=selected_bindings,
        component_artifact_digests=component_artifact_digests,
        required_hold_component_ids=required_hold_component_ids,
    )
    intent = {
        "schemaVersion": 1,
        "phase": phase,
        "requestId": "cyrene-wsource-core-runtime-" + "a" * 32,
        "previousCatalogGeneration": 5,
        "expectedCatalogGeneration": 6,
        "previousPolicyDigest": prior_policy_digest,
        "bindingScopes": binding_scopes,
        "sourceIdentity": source_identity,
        "sourceArguments": list(source_arguments),
        "runtimeBindings": runtime_bindings,
        "changed": True,
        "selectedBindings": [],
        "componentArtifactDigests": component_artifact_digests,
        "requiredHoldComponentIds": [component_id],
        "parentPlanId": plan_id,
        "parentPlanDigest": plan_digest,
    }
    transaction = {
        "planId": plan_id,
        "planDigest": plan_digest,
        "workloadId": "catalyst",
        "maintenanceToken": "held-token-private",
        "sourceUpdateIntent": intent,
    }
    transaction_path = updater._private_state_directory("transactions") / f"{plan_id}.json"
    updates._atomic_json(transaction_path, transaction)

    result = {
        "catalogGeneration": 6,
        "catalogDigest": "sha256:" + "f" * 64,
        "policyGeneration": 6,
        "policyDigest": "sha256:" + "1" * 64,
        "sourceIdentities": [],
        "bindings": [],
    }

    class RuntimeHelper:
        class WorkloadSourceUpdate:
            def __init__(self, **_fields: object) -> None:
                pass

        @staticmethod
        def reconcile_workload_source_update(_update: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def build_workload_source_update(**_kwargs: object) -> SimpleNamespace:
            return update

        @staticmethod
        def apply_workload_source_update(_update: object, **_kwargs: object) -> dict[str, object]:
            return result

    monkeypatch.setattr(
        updater,
        "_workload_source_state",
        lambda _workload_id: ({"generation": 5}, {}, {}, RuntimeHelper),
    )
    monkeypatch.setattr(updater, "_load_workload_package_runtime", lambda: RuntimeHelper)
    monkeypatch.setattr(
        updater, "_workload_hold_echo", lambda _transaction: {"transaction_id": "held"}
    )
    monkeypatch.setattr(
        updater, "_write_workload_activity_generation", lambda *_args, **_kwargs: None
    )

    applied = updater._update_workload_source_policy(
        transaction,
        transaction_path,
        source_policy={},
        selected_rows=[],
        installation_records={},
        phase=phase,
    )

    assert applied == result
    assert transaction["sourceUpdateIntents"][phase]["committed"] is True
    assert transaction["sourceUpdateIntents"][phase]["result"] == result
    assert transaction["sourceUpdateIntent"] == transaction["sourceUpdateIntents"][phase]


def test_workload_receipt_upgrade_persists_exact_raw_release_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _empty_updater(tmp_path)
    artifact_digest = "sha256:" + "a" * 64
    manifest_asset_digest = "sha256:" + "b" * 64
    manifest = {
        "schemaVersion": 2,
        "releaseId": "preview-cyrene-client-workspace-web-" + "c" * 40,
        "componentId": "cyrene-client-workspace-web",
        "version": "0.1.0",
        "channel": "preview",
        "target": {"os": "linux", "architecture": "x86_64", "runtime": "static-web"},
        "contentDigest": artifact_digest,
        "artifact": {
            "kind": "static-web",
            "format": "tar.gz",
            "uri": "https://example.invalid/release.tar.gz",
            "sha256": artifact_digest,
            "sizeBytes": 1,
            "files": {"index.html": "sha256:" + "d" * 64},
            "maxEntries": 1,
            "maxUncompressedBytes": 128,
            "entrypoint": "index.html",
        },
        "dependencies": [],
        "restart": {"group": "none"},
        "source": {
            "repository": "Example-Corp/Client",
            "ref": "refs/heads/develop",
            "commit": "e" * 40,
        },
        "provenance": {},
    }
    manifest["manifestDigest"] = updates._digest_json(manifest, "manifestDigest")
    base = {
        "componentId": manifest["componentId"],
        "version": manifest["version"],
        "releaseIdentity": manifest["manifestDigest"],
        "manifestDigest": manifest["manifestDigest"],
        "artifactDigest": artifact_digest,
        "bundleIdentity": None,
        "manifest": manifest,
    }
    updater._write_release_receipt(base)
    evidence = {
        **base,
        "releaseId": manifest["releaseId"],
        "targetId": "linux-ubuntu-24.04-x86_64-web",
        "manifestAssetDigest": manifest_asset_digest,
        "manifestUri": "https://example.invalid/component-release-manifest-v2.json",
        "releaseTag": manifest["releaseId"],
        "indexIdentity": {
            "assetName": "component-release-index-v1.json",
            "assetDigest": "sha256:" + "f" * 64,
        },
        "publisherIdentity": {
            "id": "official-client-workspace-web",
            "repository": "Example-Corp/Client",
            "workflow": "Example-Corp/Client/.github/workflows/web.yml",
            "tagFormat": "component-source-sha",
        },
        "attestationRef": {
            "repository": "Example-Corp/Client",
            "workflow": "Example-Corp/Client/.github/workflows/web.yml",
            "sourceCommit": "e" * 40,
            "subjectName": "component-release-manifest-v2.json",
            "subjectDigest": manifest_asset_digest,
        },
        "releasePath": str(tmp_path / "web" / "releases" / "release"),
        "archivePath": str(tmp_path / "stage" / "release.tar.gz"),
        "pointerIdentity": "release",
    }
    updater._write_release_receipt(evidence)
    updater._write_active_receipt(evidence)

    receipt = updater._read_release_receipt(manifest["componentId"], manifest["manifestDigest"])
    assert receipt is not None
    assert receipt["schemaVersion"] == 2
    assert receipt["manifestAssetDigest"] == manifest_asset_digest
    assert receipt["targetId"] == "linux-ubuntu-24.04-x86_64-web"
    active = updater._read_active_receipt(manifest["componentId"])
    assert active == receipt
    active_path = updater._installed_component_directory(manifest["componentId"]) / "active.json"
    assert json.loads(active_path.read_text(encoding="utf-8")) == {
        "schemaVersion": 1,
        "componentId": manifest["componentId"],
        "releaseIdentity": manifest["manifestDigest"],
        "bundleIdentity": None,
    }

    updater.components = {
        manifest["componentId"]: {
            "componentId": manifest["componentId"],
            "kind": "static-web",
        }
    }
    monkeypatch.setattr(
        updater,
        "_installed_static_web",
        lambda _component: {
            "active": True,
            "activeVersion": manifest["version"],
            "manifest": manifest,
            "manifestDigest": manifest["manifestDigest"],
            "releaseIdentity": manifest["manifestDigest"],
            "artifactDigest": artifact_digest,
            "pointerIdentity": "release",
            "bundleIdentity": None,
        },
    )
    monkeypatch.setattr(updater, "_reload_catalog_for_operation", lambda: None)
    status = updater.workload_status()
    row = next(row for row in status["components"] if row["componentId"] == manifest["componentId"])
    assert row["installed"] is True
    assert row["version"] == manifest["version"]
    assert row["releaseId"] == manifest["releaseId"]
    assert row["targetId"] == "linux-ubuntu-24.04-x86_64-web"
    assert row["manifestDigest"] == manifest["manifestDigest"]
    assert row["manifestAssetDigest"] == manifest_asset_digest
    assert row["digest"] == artifact_digest
    assert row["installationId"] is None
    assert row["verification"] == {"identityAttested": True}

    inventory, _package_inventory = updater._installed_workload_components(
        (manifest["componentId"],), workload_id="catalyst"
    )
    assert inventory[manifest["componentId"]]["releaseId"] == manifest["releaseId"]
    assert inventory[manifest["componentId"]]["manifestAssetDigest"] == manifest_asset_digest
    assert inventory[manifest["componentId"]]["targetId"] == "linux-ubuntu-24.04-x86_64-web"


def test_workspace_bootstrap_uses_the_compiled_catalog_authority_pin(tmp_path: Path) -> None:
    updater = updates.ComponentUpdater(
        catalog_path=updates.DEFAULT_CATALOG,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "update-state",
        install_root=tmp_path / "install",
        release_lock_path=tmp_path / "missing-release-lock.json",
        broker_path=tmp_path / "missing-broker",
        load_active_catalog=False,
    )

    assert updater.bootstrap_catalog_digest == updates.TRUSTED_CATALOG_DIGEST
    assert updater.catalog_generation == 13


@pytest.mark.parametrize(
    ("tag_format", "prefix", "version", "expected_tag"),
    [
        ("source-sha", "preview-example-component-", None, "preview-example-component-" + "a" * 40),
        (
            "component-source-sha",
            "preview-example-component-",
            None,
            "preview-example-component-" + "a" * 40,
        ),
        (
            "component-version-source-sha",
            "preview-example-component-",
            "0.2.0",
            "preview-example-component-0.2.0-" + "a" * 40,
        ),
    ],
)
def test_index_release_tag_uses_exact_catalog_publisher_format(
    tmp_path: Path,
    tag_format: str,
    prefix: str,
    version: str | None,
    expected_tag: str,
) -> None:
    updater = _empty_updater(tmp_path)
    updater.catalog = {"channels": {"preview": {"sourceRefs": ["refs/heads/develop"]}}}
    component = {
        "componentId": "example-component",
        "releaseDiscovery": {
            "tagPrefixes": {
                "preview": prefix,
                "stable": "stable-example-component-",
            }
        },
    }
    publisher = {
        "repository": "Example-Corp/Release-Assets",
        "workflow": "Example-Corp/Release-Assets/.github/workflows/release.yml",
        "releaseDiscovery": {"indexAssetName": "component-release-index-v1.json"},
        "tagFormat": tag_format,
    }
    release_rows: list[dict[str, object]] = [{"componentId": "example-component"}]
    if version is not None:
        release_rows[0]["version"] = version
    index: dict[str, object] = {
        "schemaVersion": 1,
        "repository": publisher["repository"],
        "channel": "preview",
        "source": {
            "repository": "https://github.com/Example-Corp/Release-Assets",
            "ref": "refs/heads/develop",
            "commit": "a" * 40,
        },
        "provenance": {
            "attestation": {
                "kind": "github-artifact-attestation",
                "repository": publisher["repository"],
                "workflow": publisher["workflow"],
                "predicateType": "https://slsa.dev/provenance/v1",
                "subjectName": "component-release-index-v1.json",
                "run": {
                    "id": "123",
                    "attempt": 1,
                    "url": "https://github.com/Example-Corp/Release-Assets/actions/runs/123/attempts/1",
                },
            }
        },
        "releases": release_rows,
        "compatibilityGroups": [],
    }
    index["indexDigest"] = updates._digest_json(index, "indexDigest")
    updater._validate_index(
        index,
        publisher,
        "preview",
        {"tag_name": expected_tag},
        component,
    )


def test_index_release_tag_rejects_unversioned_plugin_tag_and_mixed_versions(
    tmp_path: Path,
) -> None:
    updater = _empty_updater(tmp_path)
    updater.catalog = {"channels": {"preview": {"sourceRefs": ["refs/heads/develop"]}}}
    component = {
        "componentId": "example-component",
        "releaseDiscovery": {
            "tagPrefixes": {
                "preview": "preview-example-component-",
                "stable": "stable-example-component-",
            }
        },
    }
    publisher = {
        "repository": "Example-Corp/Release-Assets",
        "workflow": "Example-Corp/Release-Assets/.github/workflows/release.yml",
        "releaseDiscovery": {"indexAssetName": "component-release-index-v1.json"},
        "tagFormat": "component-version-source-sha",
    }
    index: dict[str, object] = {
        "schemaVersion": 1,
        "repository": publisher["repository"],
        "channel": "preview",
        "source": {
            "repository": "https://github.com/Example-Corp/Release-Assets",
            "ref": "refs/heads/develop",
            "commit": "a" * 40,
        },
        "provenance": {
            "attestation": {
                "kind": "github-artifact-attestation",
                "repository": publisher["repository"],
                "workflow": publisher["workflow"],
                "predicateType": "https://slsa.dev/provenance/v1",
                "subjectName": "component-release-index-v1.json",
                "run": {
                    "id": "123",
                    "attempt": 1,
                    "url": "https://github.com/Example-Corp/Release-Assets/actions/runs/123/attempts/1",
                },
            }
        },
        "releases": [
            {"componentId": "example-component", "version": "0.2.0"},
            {"componentId": "example-component", "version": "0.3.0"},
        ],
        "compatibilityGroups": [],
    }
    index["indexDigest"] = updates._digest_json(index, "indexDigest")
    with pytest.raises(updates.UpdateError, match="one exact component version"):
        updater._validate_index(
            index,
            publisher,
            "preview",
            {"tag_name": "preview-example-component-0.2.0-" + "a" * 40},
            component,
        )


def test_workload_protocol_defaults_check_action_and_repeats_action_on_stage_apply() -> None:
    updater = object.__new__(updates.ComponentUpdater)
    updater._require_authorized_process = lambda: None
    calls: list[tuple[str, str | None]] = []
    updater.check_workload = lambda workload_id, target_id, selections, *, action: (
        calls.append(("check", action)) or {"status": "ready"}
    )
    updater.stage_workload = lambda workload_id, target_id, plan_id, plan_digest, *, action: (
        calls.append(("stage", action)) or {"status": "staged"}
    )
    updater.apply_workload = (
        lambda workload_id, target_id, plan_id, plan_digest, confirmation, *, action: (
            calls.append(("apply", action)) or {"status": "installed"}
        )
    )

    common = {
        "protocolVersion": updates.WORKLOAD_PROTOCOL_VERSION,
        "workloadId": "plugins",
        "targetId": updates.WORKLOAD_HOST_TARGET,
    }
    check = updater.handle_workload(
        {
            **common,
            "operation": "check",
            "selections": {"includeComponentIds": [], "excludeComponentIds": [], "choices": {}},
        }
    )
    uninstall_check = updater.handle_workload(
        {
            **common,
            "operation": "check",
            "action": "uninstall",
            "selections": {
                "includeComponentIds": ["cyrene-tools-dataset-preparation"],
                "excludeComponentIds": [],
                "choices": {},
            },
        }
    )
    stage = updater.handle_workload(
        {
            **common,
            "operation": "stage",
            "action": "uninstall",
            "planId": "plan-example",
            "planDigest": "sha256:" + "a" * 64,
        }
    )
    apply = updater.handle_workload(
        {
            **common,
            "operation": "apply",
            "action": "uninstall",
            "planId": "plan-example",
            "planDigest": "sha256:" + "a" * 64,
            "confirmation": {
                "planId": "plan-example",
                "planDigest": "sha256:" + "a" * 64,
                "confirmed": True,
            },
        }
    )

    assert check["ok"] is True and check["result"]["status"] == "ready"
    assert uninstall_check["ok"] is True
    assert stage["ok"] is True and apply["ok"] is True
    assert calls == [
        ("check", "install"),
        ("check", "uninstall"),
        ("stage", "uninstall"),
        ("apply", "uninstall"),
    ]


def test_workload_protocol_rejects_stage_without_repeated_action() -> None:
    updater = object.__new__(updates.ComponentUpdater)
    response = updater.handle_workload(
        {
            "protocolVersion": updates.WORKLOAD_PROTOCOL_VERSION,
            "operation": "stage",
            "workloadId": "catalyst",
            "targetId": updates.WORKLOAD_HOST_TARGET,
            "planId": "plan-example",
            "planDigest": "sha256:" + "a" * 64,
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    ("profile_id", "version", "abi"),
    [
        ("linux-ubuntu-22.04-x86_64-python-3.12", "22.04", "glibc-2.35"),
        ("linux-ubuntu-24.04-x86_64-python-3.12", "24.04", "glibc-2.39"),
    ],
)
def test_product_python_target_selects_exact_host_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_id: str,
    version: str,
    abi: str,
) -> None:
    updater = updates.ComponentUpdater(
        catalog_path=updates.DEFAULT_CATALOG,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "update-state",
        install_root=tmp_path / "install",
        release_lock_path=WORKSPACE_ROOT / "release-lock.json",
        broker_path=tmp_path / "missing-broker",
        load_active_catalog=False,
    )
    lock = json.loads((WORKSPACE_ROOT / "release-lock.json").read_text(encoding="utf-8"))
    updater.native_python_profiles = lock["nativePythonProfiles"]
    monkeypatch.setattr(
        updates.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": version},
    )
    monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(updates.platform, "libc_ver", lambda: ("glibc", abi.removeprefix("glibc-")))
    component = updater.components["cyrene-navigator"]
    monkeypatch.setattr(updater, "_private_python_runtime_ready", lambda profile: False)
    assert updater._target_for(component) is None

    monkeypatch.setattr(updater, "_private_python_runtime_ready", lambda profile: True)
    target = updater._target_for(component)

    assert target is not None
    assert target["id"] == profile_id
    assert target["artifactKind"] == "python-bundle"


def test_portable_data_target_remains_independent_of_ubuntu_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = updates.ComponentUpdater(
        catalog_path=updates.DEFAULT_CATALOG,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "update-state",
        install_root=tmp_path / "install",
        release_lock_path=tmp_path / "missing-release-lock.json",
        broker_path=tmp_path / "missing-broker",
        load_active_catalog=False,
    )
    monkeypatch.setattr(
        updates.platform,
        "freedesktop_os_release",
        lambda: {"ID": "fedora", "VERSION_ID": "42"},
    )
    monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(updates.platform, "libc_ver", lambda: ("glibc", "2.39"))

    target = updater._target_for(updater.components["cyrene-product-contract-bundle"])

    assert target is not None
    assert target["id"] == "portable-contract-data-v1"
    assert target["artifactKind"] == "data-bundle"


def _configure_unmanaged_runtime_agent(
    updater: updates.ComponentUpdater,
    unit_dir: Path,
    *,
    distribution_version: str = "24.04",
    abi: str = "glibc-2.39",
) -> dict[str, object]:
    target_id = "linux-test-x86_64-systemd"
    component_id = "cy-runtime-agent"
    updater.targets[target_id] = {
        "target": {
            "os": "linux",
            "distribution": "ubuntu",
            "distributionVersion": distribution_version,
            "architecture": updates.platform.machine(),
            "abi": abi,
            "runtime": "systemd",
        }
    }
    updater.systemd_unit_dirs = (unit_dir,)
    component: dict[str, object] = {
        "componentId": component_id,
        "kind": "native-binary",
        "targets": [
            {
                "targetId": target_id,
                "artifactKind": "native-binary",
                "support": "supported",
            }
        ],
        "restart": {"group": "core-runtime", "unit": "cy-runtime-agent.service", "order": 41},
        "systemdUnit": "cy-runtime-agent.service",
    }
    updater.components[component_id] = component
    return component


@pytest.mark.parametrize(
    ("distribution_version", "abi"),
    [("22.04", "glibc-2.35"), ("24.04", "glibc-2.39")],
)
def test_os_supported_unmanaged_native_has_no_apply_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    distribution_version: str,
    abi: str,
) -> None:
    updater = _empty_updater(tmp_path)
    monkeypatch.setattr(
        updates.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": distribution_version},
    )
    monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        updates.platform,
        "libc_ver",
        lambda: ("glibc", abi.removeprefix("glibc-")),
    )
    component = _configure_unmanaged_runtime_agent(
        updater,
        tmp_path / "missing-units",
        distribution_version=distribution_version,
        abi=abi,
    )

    row = updater._result_component(
        component,
        {"status": "READY", "gate_generation": 1, "blocker_codes": []},
        phase="available",
        available_version="1.0.0",
        update_available=True,
    )

    assert row["supported"] is True
    assert row["target"]["runtime"] == "systemd"
    assert row["allowedActions"] == ["check", "stage"]
    assert row["blockers"][0]["code"] == "SERVICE_NOT_MANAGED"


def test_unmanaged_native_apply_refuses_before_begin_or_pointer_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _empty_updater(tmp_path)
    _configure_unmanaged_runtime_agent(updater, tmp_path / "missing-units")
    component_id = "cy-runtime-agent"
    plan_id = "plan-" + "a" * 32
    plan_digest = "sha256:" + "b" * 64
    artifact_digest = "sha256:" + "c" * 64
    release_root = updater.install_root / "components" / component_id
    releases = release_root / "releases"
    releases.mkdir(parents=True)
    old_pointer = "releases/0.9.0--" + "d" * 64
    (releases / old_pointer.removeprefix("releases/")).mkdir()
    active = release_root / "active"
    active.symlink_to(old_pointer)

    stage_directory = updater._private_state_directory("staged") / plan_id
    stage_directory.mkdir(mode=0o700)
    (stage_directory / "stage.json").write_text(
        json.dumps(
            {
                "phase": "staged",
                "channel": "stable",
                "plan": {"planId": plan_id, "planDigest": plan_digest},
                "components": [
                    {
                        "componentId": component_id,
                        "version": "1.0.0",
                        "manifestDigest": "sha256:" + "e" * 64,
                        "artifactDigest": artifact_digest,
                        "restartGroup": "core-runtime",
                        "manifest": {"version": "1.0.0"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(updater, "_validate_staged_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    monkeypatch.setattr(
        updater,
        "_readiness_for",
        lambda *args, **kwargs: pytest.fail(
            "unmanaged components must be rejected before readiness or Begin"
        ),
    )
    monkeypatch.setattr(
        updater,
        "_broker_request",
        lambda *args, **kwargs: pytest.fail("unmanaged components must not call BeginMaintenance"),
    )

    confirmation = {"planId": plan_id, "planDigest": plan_digest, "confirmed": True}
    with pytest.raises(updates.UpdateError) as error:
        updater.apply(plan_id, plan_digest, confirmation, channel="stable")

    assert error.value.code == "SERVICE_NOT_MANAGED"
    assert os.readlink(active) == old_pointer
    assert not (updater.state_root / "transactions" / f"{plan_id}.json").exists()


@pytest.mark.parametrize(
    ("version", "version_range", "expected"),
    [
        ("0.1.0", "=0.1.0", True),
        ("0.1.9", ">=0.1.0, <0.2.0", True),
        ("0.2.0", ">=0.1.0, <0.2.0", False),
    ],
)
def test_supported_dependency_ranges_are_exact_and_bounded(
    version: str, version_range: str, expected: bool
) -> None:
    assert updates._version_satisfies(version, version_range) is expected


@pytest.mark.parametrize(
    "version_range",
    ["^0.1.0", "~0.1", ">=0.1.0,<0.2.0", ">=0.2.0, <0.1.0", "latest"],
)
def test_unknown_or_malformed_dependency_ranges_fail_closed(version_range: str) -> None:
    with pytest.raises(updates.UpdateError, match="component versionRange"):
        updates._parse_supported_version_range(version_range)


def test_fixed_json_protocol_rejects_arbitrary_fields_and_extra_lines(tmp_path: Path) -> None:
    updater = _empty_updater(tmp_path)
    invalid = updater.handle(
        {
            "protocolVersion": updates.PROTOCOL_VERSION,
            "operation": "status",
            "executable": "/bin/sh",
        }
    )
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "INVALID_REQUEST"

    output = io.StringIO()
    updates.run_json_stdio(
        updater,
        io.StringIO(
            json.dumps({"protocolVersion": updates.PROTOCOL_VERSION, "operation": "status"})
            + "\n{}\n"
        ),
        output,
    )
    envelope = json.loads(output.getvalue())
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INVALID_REQUEST"


def test_empty_activity_catalog_is_a_valid_zero_owner_scope(tmp_path: Path) -> None:
    updater = _empty_updater(tmp_path)
    updater.activity_catalog_path.write_text(
        json.dumps({"schema_version": 1, "generation": 1, "sources": []}),
        encoding="utf-8",
    )

    catalog, source_ids = updater._activity_catalog()

    assert catalog["sources"] == []
    assert source_ids == []


def test_apply_confirmation_is_bound_to_plan_id_and_digest(tmp_path: Path) -> None:
    updater = _empty_updater(tmp_path)
    plan_id = "plan-" + "a" * 32
    plan_digest = "sha256:" + "b" * 64

    with pytest.raises(updates.UpdateError) as error:
        updater.apply(
            plan_id,
            plan_digest,
            {"planId": plan_id, "planDigest": plan_digest, "confirmed": False},
        )

    assert error.value.code == "CONFIRMATION_MISMATCH"


@pytest.mark.parametrize(
    ("distribution_version", "abi"),
    [("22.04", "glibc-2.35"), ("24.04", "glibc-2.39")],
)
def test_begin_payload_preserves_sha256_artifact_digest_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    distribution_version: str,
    abi: str,
) -> None:
    updater = _empty_updater(tmp_path)
    component_id = "cyrene-test"
    plan_id = "plan-" + "a" * 32
    plan_digest = "sha256:" + "b" * 64
    artifact_digest = "sha256:" + "c" * 64
    target_id = "linux-test-x86_64-systemd"
    updater.targets[target_id] = {
        "target": {
            "os": "linux",
            "distribution": "ubuntu",
            "distributionVersion": distribution_version,
            "architecture": "x86_64",
            "abi": abi,
            "runtime": "systemd",
        }
    }
    monkeypatch.setattr(
        updates.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": distribution_version},
    )
    monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        updates.platform,
        "libc_ver",
        lambda: ("glibc", abi.removeprefix("glibc-")),
    )
    unit_directory = tmp_path / "units"
    unit_directory.mkdir()
    (unit_directory / "cyrene-test.service").write_text(
        "[Service]\nExecStart=/usr/bin/cyrene component-run cyrene-test\n",
        encoding="utf-8",
    )
    (unit_directory / "cyrene-test.service").chmod(0o644)
    updater.systemd_unit_dirs = (unit_directory,)
    updater.components[component_id] = {
        "componentId": component_id,
        "kind": "native-binary",
        "targets": [
            {"targetId": target_id, "artifactKind": "native-binary", "support": "supported"}
        ],
        "restart": {"group": "single-service", "unit": "cyrene-test.service"},
        "systemdUnit": "cyrene-test.service",
    }
    stage_directory = updater._private_state_directory("staged") / plan_id
    stage_directory.mkdir(mode=0o700)
    stage_path = stage_directory / "stage.json"
    stage_path.write_text(
        json.dumps(
            {
                "phase": "staged",
                "channel": "stable",
                "plan": {"planId": plan_id, "planDigest": plan_digest},
                "components": [
                    {
                        "componentId": component_id,
                        "version": "1.0.0",
                        "manifestDigest": "sha256:" + "d" * 64,
                        "artifactDigest": artifact_digest,
                        "restartGroup": "single-service",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    monkeypatch.setattr(updater, "_validate_staged_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(updater, "_unit_exists", lambda component: True)
    monkeypatch.setattr(
        updater,
        "_readiness_for",
        lambda *args, **kwargs: {
            "status": "READY",
            "gate_generation": 4,
            "install_catalog_generation": 9,
            "active_task_count": 0,
            "active_tasks": [],
            "unknown_activity_sources": [],
            "active_worker_count": 0,
            "active_allocation_count": 0,
            "inflight_runtime_admission_count": 0,
            "requires_restart_confirmation": True,
        },
    )
    monkeypatch.setattr(
        updater,
        "_activity_catalog",
        lambda: ({"generation": 9, "sources": []}, ["cyrene-test"]),
    )
    monkeypatch.setattr(
        updater,
        "_capture_active_versions",
        lambda components: [{"componentId": component_id, "version": None}],
    )
    monkeypatch.setattr(updater, "_activate_transaction", lambda transaction: None)
    monkeypatch.setattr(updater, "_restart_transaction", lambda transaction: None)
    monkeypatch.setattr(updater, "_health_transaction", lambda transaction: None)
    monkeypatch.setattr(updater, "_applied_result", lambda transaction: {"status": "applied"})

    begin_payload: dict[str, object] = {}
    end_payload: dict[str, object] = {}
    begin_attempts = 0

    def broker_request(method: str, params: dict[str, object], *, request_id: str | None = None):
        nonlocal begin_attempts
        if method == "BeginMaintenance":
            begin_attempts += 1
            if begin_attempts == 1:
                return dict(BROKER_BEGIN_NO_TOKEN_RESULTS["STALE_READINESS"])
            begin_payload.update(params)
            assert params["request_id"] == request_id
            return {
                "status": "MAINTENANCE_ACTIVE",
                "maintenance_token": "t" * 32,
                "gate_generation": 8,
                "blocker_codes": [],
            }
        if method == "EndMaintenance":
            end_payload.update(params)
            end_payload["envelope_request_id"] = request_id
            return {"status": "SUCCESS"}
        raise AssertionError(f"unexpected broker method {method}")

    monkeypatch.setattr(updater, "_broker_request", broker_request)
    confirmation = {"planId": plan_id, "planDigest": plan_digest, "confirmed": True}
    transaction_path = updater.state_root / "transactions" / f"{plan_id}.json"
    with pytest.raises(
        updates.UpdateError, match="changed readiness before the maintenance gate"
    ) as error:
        updater.apply(plan_id, plan_digest, confirmation, channel="stable")

    assert error.value.code == "STALE_READINESS"
    assert error.value.maintenance_not_acquired is True
    assert not transaction_path.exists()
    assert stage_path.is_file()

    result = updater.apply(
        plan_id,
        plan_digest,
        confirmation,
        channel="stable",
    )

    assert result == {"status": "applied"}
    assert begin_attempts == 2
    assert begin_payload["component_artifact_digests"] == {component_id: artifact_digest}
    assert begin_payload["request_id"] == "cyrene-update-" + plan_id
    assert end_payload["request_id"] == begin_payload["request_id"]
    assert end_payload["target_kind"] == begin_payload["target_kind"]
    assert end_payload["envelope_request_id"] == f"cyrene-update-end-{plan_id}-success"


def test_begin_no_token_fixtures_cover_all_broker_refusal_statuses() -> None:
    assert updates.BEGIN_NO_TOKEN_STATUSES == BROKER_READINESS_STATUS_WIRE_VALUES - {"READY"}
    assert set(BROKER_BEGIN_NO_TOKEN_RESULTS) == updates.BEGIN_NO_TOKEN_STATUSES


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        ("ACTIVE_TASKS", "ACTIVE_TASKS"),
        ("UNKNOWN", "GATE_UNKNOWN"),
        ("IDLE_RUNTIME_REQUIRES_UNLOAD", "IDLE_RUNTIME_REQUIRES_UNLOAD"),
        ("MAINTENANCE_ACTIVE", "MAINTENANCE_ACTIVE"),
        ("STALE_READINESS", "STALE_READINESS"),
        ("USER_CONFIRMATION_REQUIRED", "USER_CONFIRMATION_REQUIRED"),
    ],
)
def test_begin_normal_no_token_status_is_classified_as_no_acquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected_code: str,
) -> None:
    updater = _empty_updater(tmp_path)
    transaction = {
        "planId": "plan-" + "a" * 32,
        "requestId": "cyrene-update-plan-" + "a" * 32,
        "planDigest": "sha256:" + "b" * 64,
        "componentArtifactDigests": {"cyrene-test": "sha256:" + "c" * 64},
        "phase": "begin_pending",
        "targetKind": "CORE_RUNTIME"
        if status == "IDLE_RUNTIME_REQUIRES_UNLOAD"
        else "PACKAGE_ONLY",
        "expectedCatalogGeneration": 9,
        "expectedActivitySources": ["cyrene-test"],
        "expectedGateGeneration": 7,
    }
    monkeypatch.setattr(
        updater,
        "_broker_request",
        lambda *args, **kwargs: dict(BROKER_BEGIN_NO_TOKEN_RESULTS[status]),
    )

    with pytest.raises(updates.UpdateError) as error:
        updater._begin_maintenance(transaction)

    assert error.value.code == expected_code
    assert error.value.maintenance_not_acquired is True


@pytest.mark.parametrize(
    "result",
    [
        {"status": "READY", "maintenance_token": None, "gate_generation": 7, "blocker_codes": []},
        {
            "status": "FUTURE_STATUS",
            "maintenance_token": None,
            "gate_generation": 7,
            "blocker_codes": [],
        },
        {
            "status": "STALE_READINESS",
            "gate_generation": 7,
            "blocker_codes": ["READINESS_GENERATION_STALE"],
        },
    ],
)
def test_begin_unknown_or_malformed_result_keeps_transaction_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, object],
) -> None:
    updater = _empty_updater(tmp_path)
    transaction = {
        "planId": "plan-" + "a" * 32,
        "requestId": "cyrene-update-plan-" + "a" * 32,
        "planDigest": "sha256:" + "b" * 64,
        "componentArtifactDigests": {"cyrene-test": "sha256:" + "c" * 64},
        "phase": "begin_pending",
        "targetKind": "PACKAGE_ONLY",
        "expectedCatalogGeneration": 9,
        "expectedActivitySources": ["cyrene-test"],
        "expectedGateGeneration": 7,
    }
    monkeypatch.setattr(updater, "_broker_request", lambda *args, **kwargs: dict(result))

    with pytest.raises(updates.UpdateError) as error:
        updater._begin_maintenance(transaction)

    assert error.value.code == "GATE_UNKNOWN"
    assert error.value.maintenance_not_acquired is False


def test_begin_invalid_argument_clears_unacquired_journal_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _empty_updater(tmp_path)
    plan_id = "plan-" + "a" * 32
    plan_digest = "sha256:" + "b" * 64
    transaction = {
        "planId": plan_id,
        "requestId": "cyrene-update-" + plan_id,
        "planDigest": plan_digest,
        "componentArtifactDigests": {"cyrene-test": "sha256:" + "c" * 64},
        "phase": "begin_pending",
        "targetKind": "PACKAGE_ONLY",
        "expectedCatalogGeneration": 9,
        "expectedActivitySources": ["cyrene-test"],
        "expectedGateGeneration": 4,
    }

    def refused_request(*args, **kwargs):
        raise updates.UpdateError("INVALID_ARGUMENT", "broker rejected malformed Begin parameters")

    monkeypatch.setattr(updater, "_broker_request", refused_request)
    with pytest.raises(updates.UpdateError) as error:
        updater._begin_maintenance(transaction)

    assert error.value.maintenance_not_acquired is True
    transaction_directory = updater._private_state_directory("transactions")
    transaction_path = transaction_directory / f"{plan_id}.json"
    updates._atomic_json(transaction_path, transaction)

    updater._clear_begin_pending(transaction, transaction_path)

    assert not transaction_path.exists()


def test_workload_begin_refusal_keeps_parent_journal_and_phase_identity(
    tmp_path: Path,
) -> None:
    updater = _empty_updater(tmp_path)
    plan_id = "plan-" + "a" * 32
    transaction_path = updater._private_state_directory("transactions") / f"{plan_id}.json"
    transaction = {
        "transactionKind": "workload-assembly.v1",
        "planId": plan_id,
        "planDigest": "sha256:" + "b" * 64,
        "maintenancePhase": "package-only",
        "maintenanceRequestId": f"cyrene-workload-package-only-{plan_id}",
        "phase": "begin_pending",
        "targetKind": "PACKAGE_ONLY",
        "componentArtifactDigests": {"cyrene-tools-example": "sha256:" + "c" * 64},
    }
    updates._atomic_json(transaction_path, transaction)

    updater._clear_begin_pending(transaction, transaction_path)

    journal = json.loads(transaction_path.read_text(encoding="utf-8"))
    assert journal["planId"] == plan_id
    assert journal["phase"] == "begin_pending"
    assert journal["maintenanceHolds"]["package-only"] == {
        "requestId": f"cyrene-workload-package-only-{plan_id}",
        "status": "not_acquired",
    }


@pytest.mark.parametrize("target_kind", ["PACKAGE_ONLY", "CORE_RUNTIME"])
def test_end_payload_carries_stable_request_id_and_target_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
) -> None:
    updater = _empty_updater(tmp_path)
    transaction = {
        "planId": "plan-" + "a" * 32,
        "requestId": "cyrene-update-" + "plan-" + "a" * 32,
        "targetKind": target_kind,
        "maintenanceToken": "t" * 32,
    }
    captured: dict[str, object] = {}

    def broker_request(method: str, params: dict[str, object], *, request_id: str | None = None):
        captured.update(method=method, params=params, envelope_request_id=request_id)
        return {"status": "SUCCESS"}

    monkeypatch.setattr(updater, "_broker_request", broker_request)

    updater._end_maintenance(transaction, outcome="SUCCESS", healthy=True)

    params = captured["params"]
    assert captured["method"] == "EndMaintenance"
    assert isinstance(params, dict)
    assert params["request_id"] == transaction["requestId"]
    assert captured["envelope_request_id"] == f"cyrene-update-end-{transaction['planId']}-success"
    assert params["target_kind"] == target_kind


@pytest.mark.parametrize(
    "phase",
    [
        "core-runtime-install",
        "package-only",
        "core-runtime-activate",
        "core-runtime-uninstall",
    ],
)
def test_workload_maintenance_uses_unique_phase_request_ids(phase: str) -> None:
    plan_id = "plan-" + "a" * 32
    transaction = {
        "transactionKind": "workload-assembly.v1",
        "planId": plan_id,
        "maintenancePhase": phase,
    }

    assert updates._maintenance_request_id(transaction) == f"cyrene-workload-{phase}-{plan_id}"
    assert transaction["maintenanceRequestId"] == f"cyrene-workload-{phase}-{plan_id}"


def test_workload_end_maintenance_uses_phase_scoped_idempotency_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _empty_updater(tmp_path)
    plan_id = "plan-" + "a" * 32
    transaction = {
        "transactionKind": "workload-assembly.v1",
        "planId": plan_id,
        "maintenancePhase": "package-only",
        "maintenanceRequestId": f"cyrene-workload-package-only-{plan_id}",
        "targetKind": "PACKAGE_ONLY",
        "maintenanceToken": "t" * 32,
    }
    captured: dict[str, object] = {}

    def broker_request(method: str, params: dict[str, object], *, request_id: str | None = None):
        captured.update(method=method, params=params, envelope_request_id=request_id)
        return {"status": "SUCCESS"}

    monkeypatch.setattr(updater, "_broker_request", broker_request)

    updater._end_maintenance(transaction, outcome="SUCCESS", healthy=True)

    params = captured["params"]
    assert captured["method"] == "EndMaintenance"
    assert isinstance(params, dict)
    assert params["request_id"] == transaction["maintenanceRequestId"]
    assert captured["envelope_request_id"] == (
        f"cyrene-workload-end-package-only-{plan_id}-success"
    )


def test_privileged_helper_has_a_fixed_root_only_command() -> None:
    helper = HELPER_PATH.read_text(encoding="utf-8")
    mode = stat.S_IMODE(HELPER_PATH.stat().st_mode)

    assert mode == 0o755
    assert 'if [ "$(/usr/bin/id -u)" -ne 0 ]' in helper
    assert "exec /usr/bin/cyrene update --json" in helper
    assert '"$1" = "placement-peer-plan"' in helper
    assert '"$1" = "placement-peer-receipt"' in helper
    assert 'exec /usr/bin/cyrene placement-peer-evidence "$@"' in helper
    assert helper.count('"$@"') == 1
    pinned_gh = helper.index("gh=/usr/libexec/cyrene-tools/gh")
    pinned_gh_hash_check = helper.index('/usr/bin/sha256sum "$gh"')
    peer_dispatch = helper.index('if [ "$mode" = "peer" ]')
    assert pinned_gh < pinned_gh_hash_check < peer_dispatch
    assert helper.index("PATH=/usr/libexec/cyrene-tools:") < peer_dispatch
    assert (
        subprocess.run(
            [str(HELPER_PATH), "--executable", "/bin/sh"],
            check=False,
            capture_output=True,
            text=True,
        ).returncode
        != 0
    )


def test_polkit_policy_requires_active_admin_authentication() -> None:
    policy = ET.parse(POLICY_PATH).getroot()
    action = policy.find("action")
    assert policy.tag == "policyconfig"
    assert action is not None
    assert action.attrib["id"] == "org.cyrene.component-update"
    defaults = action.find("defaults")
    assert defaults is not None
    assert defaults.findtext("allow_any") == "no"
    assert defaults.findtext("allow_inactive") == "no"
    assert defaults.findtext("allow_active") == "auth_admin_keep"
    annotations = {item.attrib.get("key"): item.text for item in action.findall("annotate")}
    assert annotations["org.freedesktop.policykit.exec.path"] == (
        "/usr/libexec/cyrene-component-update-helper"
    )


def test_deb_installs_root_helper_and_polkit_action() -> None:
    builder = DEB_BUILDER_PATH.read_text(encoding="utf-8")
    assert '"${STAGE_DIR}/usr/libexec"' in builder
    assert '"${STAGE_DIR}/usr/share/polkit-1/actions"' in builder
    assert "cyrene-component-update-helper" in builder
    assert "org.cyrene.component-update.policy" in builder
    assert "systemd, policykit-1" in builder


def test_deb_provisions_runtime_state_directory_fail_closed() -> None:
    builder = DEB_BUILDER_PATH.read_text(encoding="utf-8")

    assert "RUNTIME_STATE_DIR=/var/lib/cyrene/runtime" in builder
    assert (
        'install -d -o root -g cyrene-runtime-maintenance -m 2770 "$RUNTIME_STATE_DIR"' in builder
    )
    assert "stat -c '%u:%g:%a' -- \"$RUNTIME_STATE_DIR\"" in builder
    assert '"0:${AUTHORITY_GID}:2770"' in builder
    assert "Refusing to repair existing runtime state in place" in builder
    assert "chown -R root:cyrene-runtime-maintenance /var/lib/cyrene/runtime" not in builder


@pytest.mark.parametrize(
    ("component_id", "contract_variable"),
    [
        ("cy-workspace-web-bff", "CYRENE_WORKSPACE_WEB_BFF_PRODUCT_CONTRACT_ROOT_V2"),
        ("cy-workspace-connector", "CYRENE_WORKSPACE_CONNECTOR_PRODUCT_CONTRACT_ROOT_V2"),
    ],
)
def test_native_runner_pins_contract_root_to_active_release(
    component_id: str,
    contract_variable: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = tmp_path / "components" / component_id / "releases" / "1.0.0"
    binary = release / "bin" / "service"
    binary.parent.mkdir(parents=True)
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)
    contracts = release / "share" / "cyrene" / "product-contracts"
    contracts.mkdir(parents=True)
    active = release.parent.parent / "active"
    active.symlink_to("releases/1.0.0")
    manifest = {
        "componentId": component_id,
        "version": "1.0.0",
        "artifact": {
            "kind": "native-binary",
            "entrypoint": "bin/service",
            "arguments": ["--manifest-arg"],
        },
    }
    manifest["manifestDigest"] = updates._digest_json(manifest, "manifestDigest")
    manifest_path = release / "component-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    for directory in (
        tmp_path,
        tmp_path / "components",
        release.parent.parent,
        release.parent,
        release,
        release / "bin",
        release / "share",
        release / "share" / "cyrene",
        contracts,
    ):
        directory.chmod(0o755)
    manifest_path.chmod(0o644)

    real_lstat = Path.lstat

    def root_owned_active(path: Path):
        info = real_lstat(path)
        if path == active or path.is_relative_to(tmp_path):
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0)
        return info

    class ExecveReached(RuntimeError):
        pass

    seen: dict[str, object] = {}

    def capture_execve(path: str, argv: list[str], environment: dict[str, str]) -> None:
        seen.update(path=path, argv=argv, environment=environment)
        raise ExecveReached

    monkeypatch.setattr(Path, "lstat", root_owned_active)
    monkeypatch.setattr(updates.os, "execve", capture_execve)
    monkeypatch.setenv(contract_variable, "/caller/controlled/path")
    other_variable = next(
        value for value in updates.PRODUCT_CONTRACT_ROOT_ENV.values() if value != contract_variable
    )
    monkeypatch.setenv(other_variable, "/caller/controlled/other")

    with pytest.raises(ExecveReached):
        updates.run_component(
            component_id,
            install_root=tmp_path,
            startup_arguments=("--unit-arg", "value"),
        )

    assert seen["path"] == str(binary)
    assert seen["argv"] == [str(binary), "--manifest-arg", "--unit-arg", "value"]
    environment = seen["environment"]
    assert isinstance(environment, dict)
    assert environment[contract_variable] == str(contracts)
    assert other_variable not in environment


def test_native_runner_requires_service_user_to_read_public_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    component_id = "cy-workspace-web-bff"
    release = tmp_path / "components" / component_id / "releases" / "1.0.0"
    binary = release / "bin" / "service"
    binary.parent.mkdir(parents=True)
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)
    active = release.parent.parent / "active"
    active.symlink_to("releases/1.0.0")
    manifest = {
        "componentId": component_id,
        "version": "1.0.0",
        "artifact": {"kind": "native-binary", "entrypoint": "bin/service", "arguments": []},
    }
    manifest["manifestDigest"] = updates._digest_json(manifest, "manifestDigest")
    manifest_path = release / "component-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    for directory in (
        tmp_path,
        tmp_path / "components",
        release.parent.parent,
        release.parent,
        release,
        release / "bin",
    ):
        directory.chmod(0o755)
    manifest_path.chmod(0o644)

    real_lstat = Path.lstat

    def root_owned_active(path: Path):
        info = real_lstat(path)
        if path == active or path.is_relative_to(tmp_path):
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0)
        return info

    real_access = updates.os.access

    def service_user_access(path: Path, mode: int, **kwargs: object) -> bool:
        if path == manifest_path and mode == updates.os.R_OK:
            return False
        return real_access(path, mode, **kwargs)

    monkeypatch.setattr(Path, "lstat", root_owned_active)
    monkeypatch.setattr(updates.os, "access", service_user_access)
    monkeypatch.setattr(
        updates.os, "execve", lambda *args, **kwargs: pytest.fail("must reject before exec")
    )

    with pytest.raises(
        updates.UpdateError, match="manifest is not root-owned and readable"
    ) as error:
        updates.run_component(component_id, install_root=tmp_path)

    assert error.value.code == "INVALID_INSTALLED_RELEASE"


def test_manifest_v2_dual_read_support() -> None:
    manifest_v2 = {
        "schemaVersion": 2,
        "releaseId": "rel-v2",
        "componentId": "cy-workspace-relay",
        "version": "0.2.0",
        "channel": "stable",
        "target": {"os": "linux", "architecture": "x86_64"},
        "protocolVersion": "cyrene.workspace.authority.v1",
        "contentDigest": "sha256:" + "b" * 64,
        "artifact": {"kind": "native-binary"},
        "dependencies": [],
        "restart": {"group": "single-service", "unit": "cy-workspace-relay.service"},
        "source": {"repository": "DoHorizon-AI/Cyrene-Plugins-Official", "commit": "a" * 40},
        "provenance": {},
    }
    manifest_v2["manifestDigest"] = updates._digest_json(manifest_v2, "manifestDigest")
    assert manifest_v2["manifestDigest"].startswith("sha256:")
