"""Echo OCI workload assembly tests over the existing updater journal."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPDATES_PATH = ROOT / "packaging" / "component_updates.py"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "workload-attestation-subjects" / "echo-d5"
ECHO_ID = "cyrene-echo"
SDK_ID = "cyrene-runtime-maintenance-sdk"
ECHO_TARGET_ID = "linux-ubuntu-24.04-x86_64-oci"
ECHO_IMAGE_REPOSITORY = "ghcr.io/dohorizon-ai/cyrene-echo"
ECHO_SOURCE_REF = "refs/heads/develop"
ECHO_SOURCE_COMMIT = "d5a920078077bdbaa76e7117ad83f69cbfa1c614"
TOKEN = "echo-test-token-" + "t" * 40
CONNECTION_REF = "opaque://plugin-supervisor/conn/private-fixture-ref"
RUNNER_IDENTITY = {
    "componentId": "cyrene-evaluation-exact-match",
    "sourceId": ECHO_ID,
    "bindingId": "catalog-binding-actual-product-cyrene-echo-cyrene-evaluation-exact-match",
    "packageId": "cyrene.evaluation.exact-match",
    "installationId": "echo-exact-match-installation-1",
}

UPDATES_SPEC = importlib.util.spec_from_file_location(
    "cyrene_modular_workload_echo_test", UPDATES_PATH
)
assert UPDATES_SPEC is not None and UPDATES_SPEC.loader is not None
updates = importlib.util.module_from_spec(UPDATES_SPEC)
sys.modules[UPDATES_SPEC.name] = updates
UPDATES_SPEC.loader.exec_module(updates)


def _sha(label: str) -> str:
    """Return a deterministic typed digest for test-owned identities."""

    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _updater(tmp_path: Path) -> Any:
    """Load the checked-in v2 Catalog into an isolated updater state root."""

    updater = updates.ComponentUpdater(
        catalog_path=ROOT / "governance" / "component-catalog-v2.json",
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "state",
        install_root=tmp_path / "install",
        broker_path=tmp_path / "missing-broker",
        trusted_catalog_digest=None,
        load_active_catalog=False,
    )
    return updater


def _fixture_candidate(updater: Any) -> tuple[Any, dict[str, Any], bytes]:
    """Read the public Echo fixture without treating it as signature evidence."""

    manifest_bytes = (FIXTURE_ROOT / "echo-ubuntu24-oci-manifest.json").read_bytes()
    index_bytes = (FIXTURE_ROOT / "component-release-index-v1.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    index = json.loads(index_bytes)
    release = next(
        row
        for row in index["releases"]
        if row["componentId"] == ECHO_ID and row["manifestDigest"] == manifest["manifestDigest"]
    )
    candidate = updates.Candidate(
        component=updater.components[ECHO_ID],
        manifest=manifest,
        manifest_digest=manifest["manifestDigest"],
        artifact_digest=manifest["artifact"]["digest"],
        manifest_uri=release["manifestUri"],
        index=index,
        index_uri=(
            "https://github.com/DoHorizon-AI/Cyrene-Echo/releases/download/"
            f"{manifest['releaseId']}/component-release-index-v1.json"
        ),
        manifest_bytes=manifest_bytes,
        release_tag=manifest["releaseId"],
        index_asset_name="component-release-index-v1.json",
        index_asset_digest=_raw_sha(index_bytes),
        manifest_asset_digest=_raw_sha(manifest_bytes),
    )
    row = {
        "componentId": ECHO_ID,
        "artifactKind": "oci-image",
        "version": manifest["version"],
        "releaseId": manifest["releaseId"],
        "manifestUri": release["manifestUri"],
        "targetId": ECHO_TARGET_ID,
        "manifestDigest": manifest["manifestDigest"],
        "manifestAssetDigest": _raw_sha(manifest_bytes),
        "digest": manifest["artifact"]["digest"],
        "indexIdentity": {
            "repository": "DoHorizon-AI/Cyrene-Echo",
            "releaseTag": manifest["releaseId"],
            "assetName": "component-release-index-v1.json",
            "assetDigest": _raw_sha(index_bytes),
        },
        "publisherIdentity": {
            "repository": "DoHorizon-AI/Cyrene-Echo",
            "workflow": "DoHorizon-AI/Cyrene-Echo/.github/workflows/component-release.yml",
        },
        "attestationRef": {
            "kind": "github-artifact-attestation",
            "runId": "37847558267",
            "attempt": 2,
            "url": "https://github.com/DoHorizon-AI/Cyrene-Echo/actions/runs/37847558267/attempts/2",
        },
        "sourcePolicy": next(
            workload["sourcePolicy"]
            for workload in updater.catalog["workloads"]
            if workload["workloadId"] == "echo"
        ),
    }
    return candidate, row, manifest_bytes


def _raw_sha(payload: bytes) -> str:
    """Hash exact fixture bytes in the release contract's typed format."""

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sdk_selected() -> dict[str, Any]:
    """Create one exact current SDK identity selected by the Echo plan."""

    digest = _sha("sdk-wheel")
    manifest_digest = _sha("sdk-manifest")
    manifest_asset_digest = _sha("sdk-manifest-asset")
    release_id = "preview-sdk-release-1"
    return {
        "componentId": SDK_ID,
        "artifactKind": "python-bundle",
        "version": "0.1.0",
        "targetId": "linux-ubuntu-24.04-x86_64-python-3.12-library",
        "releaseId": release_id,
        "manifestUri": "https://example.invalid/sdk-manifest.json",
        "manifestDigest": manifest_digest,
        "manifestAssetDigest": manifest_asset_digest,
        "digest": digest,
        "indexIdentity": {"releaseTag": release_id, "assetDigest": _sha("sdk-index")},
        "publisherIdentity": {"repository": "DoHorizon-AI/Cyrene-Platform"},
        "attestationRef": {"runId": "sdk-run-1"},
    }


def _sdk_receipt(selected: dict[str, Any]) -> dict[str, Any]:
    """Project an authenticated SDK receipt with the fields compared by updater."""

    return {
        "installed": True,
        **selected,
        "verification": {"identityAttested": True},
        "artifactDigest": selected["digest"],
        "releaseIdentity": selected["manifestDigest"],
        "releaseTag": selected["indexIdentity"]["releaseTag"],
        "sourceIdentity": {
            "artifactDigest": selected["digest"],
            "manifestDigest": selected["manifestDigest"],
            "manifestAssetDigest": selected["manifestAssetDigest"],
            "releaseId": selected["releaseId"],
        },
        "pythonPath": "/opt/cyrene/workload-operator/releases/test/venv/bin/python3.12",
    }


def _activity_catalog() -> tuple[dict[str, Any], list[str]]:
    """Supply the signed Echo source's active host identity for adapter tests."""

    catalog = {
        "schema_version": 1,
        "generation": 13,
        "sources": [
            {
                "source_id": ECHO_ID,
                "uid": 1001,
                "gid": 1001,
                "source_token_sha256": "a" * 64,
            }
        ],
    }
    return catalog, [ECHO_ID]


def _package_inventory() -> dict[str, Any]:
    """Return an unrelated active Catalyst owner for preservation assertions."""

    return {
        "components": {},
        "installationRecords": {},
        "sourceBindings": [
            {
                "sourceId": "cyrene-catalyst",
                "bindingId": "catalog-binding-catalyst-dataset-tools",
                "packageId": "cyrene.data-tools",
                "activeInstallationId": "catalyst-data-tools-installation-1",
                "state": "RUNNING",
            }
        ],
    }


def _observation(helper: Any, spec: Any, state: str = "running") -> Any:
    """Build the helper's redacted public container observation for a test spec."""

    return helper.OciContainerObservation(
        component_id=spec.component_id,
        target_id=spec.target_id,
        manifest_digest=spec.manifest_digest,
        image_digest=spec.image_digest,
        image_reference=spec.image_reference,
        container_name=spec.container_name,
        container_id="c" * 64 if state != "not-installed" else None,
        state=state,
        host_uid=spec.host_uid,
        host_gid=spec.host_gid,
        activity_source_id=spec.activity_source_id,
        activity_catalog_generation=spec.activity_catalog_generation,
        data_directory=str(spec.data_directory),
        artifact_directory=str(spec.artifact_directory),
    )


def _install_host_stubs(
    updater: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, dict[str, list[Any]]]:
    """Use the real OCI spec validator with deterministic runtime observations."""

    helper = updater._load_workload_oci_host()
    calls: dict[str, list[Any]] = {
        "pull": [],
        "start": [],
        "status": [],
        "stop": [],
        "remove": [],
    }

    def pull(spec: Any, *, runner: Any) -> dict[str, str]:
        calls["pull"].append((spec, runner))
        return {
            "imageReference": spec.image_reference,
            "imageDigest": spec.image_digest,
            "platform": "linux/amd64",
            "imageId": _sha("local-image-config"),
        }

    def start(spec: Any, *, runner: Any) -> Any:
        calls["start"].append((spec, runner))
        return _observation(helper, spec)

    def status(spec: Any, *, runner: Any) -> Any:
        calls["status"].append((spec, runner))
        return _observation(helper, spec)

    def stop(spec: Any, *, runner: Any) -> Any:
        calls["stop"].append((spec, runner))
        return _observation(helper, spec, "exited")

    def remove(spec: Any, *, runner: Any, remove_runtime_credentials: bool) -> Any:
        calls["remove"].append((spec, runner, remove_runtime_credentials))
        return _observation(helper, spec, "not-installed")

    monkeypatch.setattr(helper, "pull_and_verify_image", pull)
    monkeypatch.setattr(helper, "start_container", start)
    monkeypatch.setattr(helper, "container_status", status)
    monkeypatch.setattr(helper, "stop_container", stop)
    monkeypatch.setattr(helper, "remove_container", remove)
    monkeypatch.setattr(updater, "_load_workload_oci_host", lambda: helper)
    monkeypatch.setattr(updater, "_preflight_workload_oci_runtime", lambda: None)
    monkeypatch.setattr(updater, "_activity_catalog", _activity_catalog)
    monkeypatch.setattr(
        updater, "_read_workload_echo_api_token", lambda: (TOKEN, _sha(TOKEN), 1001, 1001)
    )
    monkeypatch.setattr(
        updater,
        "_workload_echo_runner_identity",
        lambda *_a, **_k: (CONNECTION_REF, RUNNER_IDENTITY),
    )
    monkeypatch.setattr(updater, "_workload_echo_health_status", lambda: 200)
    return helper, calls


def test_echo_staging_binds_immutable_image_target_and_publisher_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage binds the exact image/source tuple; the fake gh response is not proof."""

    updater = _updater(tmp_path)
    candidate, selected, _manifest_bytes = _fixture_candidate(updater)
    expected_statement = {
        "verificationResult": {
            "statement": {
                "predicateType": "https://slsa.dev/provenance/v1",
                "subject": [
                    {
                        "name": ECHO_IMAGE_REPOSITORY,
                        "digest": {"sha256": candidate.artifact_digest.removeprefix("sha256:")},
                    }
                ],
            }
        }
    }
    invocation: list[list[str]] = []

    def verifier(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        invocation.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps([expected_statement]), "")

    updater.runner = verifier
    monkeypatch.setattr(
        updates.shutil, "which", lambda command: "/usr/bin/gh" if command == "gh" else None
    )
    stage = updater._stage_workload_oci_image(
        candidate,
        "plan-" + "a" * 32,
        _sha("plan"),
        resolution_component=selected,
    )

    assert len(invocation) == 1
    command = invocation[0]
    assert command[3] == f"oci://{ECHO_IMAGE_REPOSITORY}@{candidate.artifact_digest}"
    assert command[command.index("--repo") + 1] == "DoHorizon-AI/Cyrene-Echo"
    assert (
        command[command.index("--signer-workflow") + 1] == selected["publisherIdentity"]["workflow"]
    )
    assert command[command.index("--source-ref") + 1] == candidate.manifest["source"]["ref"]
    assert command[command.index("--source-digest") + 1] == candidate.manifest["source"]["commit"]
    assert candidate.manifest["source"]["ref"] == ECHO_SOURCE_REF
    assert candidate.manifest["source"]["commit"] == ECHO_SOURCE_COMMIT
    assert stage["artifactKind"] == "oci-image"
    assert stage["stagedIdentity"] == {
        "planId": "plan-" + "a" * 32,
        "planDigest": _sha("plan"),
        "imageReference": f"{ECHO_IMAGE_REPOSITORY}@{candidate.artifact_digest}",
        "imageDigest": candidate.artifact_digest,
    }
    assert stage["targetId"] == ECHO_TARGET_ID
    assert stage["manifestAssetDigest"] == selected["manifestAssetDigest"]
    assert stage["artifactAttestationVerified"] is True


@pytest.mark.parametrize("mutation", ["target", "source"])
def test_echo_staging_rejects_target_or_attested_source_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """The staging adapter does not reach attestation on a target mismatch or accept a bad source."""

    updater = _updater(tmp_path)
    candidate, selected, _manifest_bytes = _fixture_candidate(updater)
    invocation: list[list[str]] = []

    if mutation == "target":
        manifest = copy.deepcopy(candidate.manifest)
        manifest["target"]["distributionVersion"] = "22.04"
        candidate = replace(candidate, manifest=manifest)
    else:
        manifest = copy.deepcopy(candidate.manifest)
        manifest["source"]["commit"] = "f" * 40
        candidate = replace(candidate, manifest=manifest)

    def verifier(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        invocation.append(arguments)
        return subprocess.CompletedProcess(arguments, 1, "", "attestation source mismatch")

    updater.runner = verifier
    monkeypatch.setattr(
        updates.shutil, "which", lambda command: "/usr/bin/gh" if command == "gh" else None
    )
    if mutation == "target":
        with pytest.raises(updates.UpdateError, match="OCI lifecycle adapter"):
            updater._stage_workload_oci_image(
                candidate,
                "plan-" + "b" * 32,
                _sha("plan"),
                resolution_component=selected,
            )
        assert invocation == []
    else:
        with pytest.raises(updates.UpdateError) as error:
            updater._stage_workload_oci_image(
                candidate,
                "plan-" + "b" * 32,
                _sha("plan"),
                resolution_component=selected,
            )
        assert error.value.code == "ATTESTATION_INVALID"
        assert invocation[0][invocation[0].index("--source-digest") + 1] == "f" * 40


def test_echo_check_blocks_missing_oci_runtime_without_persisting_plan(tmp_path: Path) -> None:
    """A host without its managed Docker runtime gets a blocker, not a stageable plan."""

    updater = _updater(tmp_path)
    plan_id = "plan-" + "f" * 32
    plan_digest = _sha("echo-check-plan")
    resolution = {
        "status": "ready",
        "planId": plan_id,
        "planDigest": plan_digest,
        "selectedComponents": [{"componentId": ECHO_ID, "artifactKind": "oci-image"}],
        "warnings": [],
        "blockers": [],
    }
    updater._reload_catalog_for_operation = lambda: None
    updater._require_workload_target = lambda workload, target: (workload, target)
    updater._build_workload_plan = lambda *_args, **_kwargs: (resolution, {}, _package_inventory())

    def missing_runtime() -> None:
        raise updates.UpdateError("OCI_RUNTIME_UNAVAILABLE", "managed Docker is unavailable")

    updater._preflight_workload_oci_runtime = missing_runtime

    result = updater.check_workload("echo", updates.WORKLOAD_HOST_TARGET, {}, channel="preview")

    plan_path = updater.state_root / "plans" / "workloads" / f"{plan_id}.json"
    assert result["status"] == "blocked"
    assert result["blockers"] == [
        {"code": "OCI_RUNTIME_UNAVAILABLE", "message": "managed Docker is unavailable"}
    ]
    assert not plan_path.exists()


def test_package_binding_journal_never_persists_connection_ref(
    tmp_path: Path,
) -> None:
    """Persist owner outcomes without copying opaque runtime credentials to the journal."""

    updater = _updater(tmp_path)
    transaction_path = tmp_path / "transaction.json"
    transaction: dict[str, Any] = {
        "planId": "plan-" + "f" * 32,
        "sdkEnvironment": {
            "pythonPath": "/opt/cyrene/workload-operator/releases/current/venv/bin/python"
        },
        "bindingOperations": [],
    }
    component = {
        "componentId": "cyrene-evaluation-exact-match",
        "bindingId": RUNNER_IDENTITY["bindingId"],
        "packageId": RUNNER_IDENTITY["packageId"],
        "sourcePolicy": {"mode": "standaloneOperator", "sourceId": ECHO_ID},
    }
    source_principals = {
        ECHO_ID: {"uid": 1001, "gid": 1001, "tokenPath": tmp_path / "source.token"}
    }

    def run_binding_operation(**kwargs: Any) -> dict[str, str]:
        request_id = kwargs["request_id"]
        kwargs["persist_intent"](
            request_id,
            {
                "binding_id": component["bindingId"],
                "package_id": component["packageId"],
                "installation_id": RUNNER_IDENTITY["installationId"],
                "operation": "activate",
            },
        )
        kwargs["persist_outcome"](
            {
                "binding_id": component["bindingId"],
                "installation_id": RUNNER_IDENTITY["installationId"],
                "state": "RUNNING",
                "connection_ref": CONNECTION_REF,
            },
            {"request_id": request_id, "ok": True},
        )
        return {"requestId": request_id}

    updater._workload_binding_operation(
        transaction,
        transaction_path,
        operation="activate",
        component=component,
        installation_id=RUNNER_IDENTITY["installationId"],
        activity_catalog={"generation": 13},
        runtime_policy={"schema_version": 1, "generation": 13, "sources": []},
        source_principals=source_principals,
        helper=SimpleNamespace(run_package_binding_operation=run_binding_operation),
    )

    persisted = transaction_path.read_text(encoding="utf-8")
    assert CONNECTION_REF not in persisted
    assert "connection_ref" not in persisted
    assert json.loads(persisted)["bindingOperations"][0]["status"]["state"] == "RUNNING"


def test_echo_apply_status_and_uninstall_keep_product_state_scoped_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apply/status/uninstall expose Echo identity while preserving Catalyst state and owners."""

    updater = _updater(tmp_path)
    candidate, echo_selected, _manifest_bytes = _fixture_candidate(updater)
    sdk_selected = _sdk_selected()
    sdk_identity = _sdk_receipt(sdk_selected)
    plan_id = "plan-" + "d" * 32
    plan_digest = _sha("echo-assembly-plan")
    inventory = _package_inventory()
    resolution = {
        "status": "ready",
        "planDigest": plan_digest,
        "selectedComponents": [echo_selected, sdk_selected],
        "sourceBindings": copy.deepcopy(inventory["sourceBindings"]),
        "warnings": [],
        "blockers": [],
    }
    staged_echo = {
        "componentId": ECHO_ID,
        "status": "staged",
        "artifactKind": "oci-image",
        "artifactAttestationVerified": True,
        "stagedIdentity": {
            "planId": plan_id,
            "planDigest": plan_digest,
            "imageReference": f"{ECHO_IMAGE_REPOSITORY}@{echo_selected['digest']}",
            "imageDigest": echo_selected["digest"],
        },
    }
    staged_sdk = {"componentId": SDK_ID, "status": "current"}
    stored = {
        "planId": plan_id,
        "planDigest": plan_digest,
        "workloadId": "echo",
        "targetId": updates.WORKLOAD_HOST_TARGET,
        "catalogDigest": updater.catalog_digest,
        "catalogGeneration": updater.catalog_generation,
        "channel": "preview",
        "selections": {},
        "stagedComponents": [staged_echo, staged_sdk],
    }
    monkeypatch.setattr(
        updater,
        "_build_workload_plan",
        lambda *_args, **_kwargs: (
            resolution,
            {ECHO_ID: candidate},
            copy.deepcopy(inventory),
        ),
    )
    monkeypatch.setattr(
        updater,
        "_load_workload_sdk_environment",
        lambda: SimpleNamespace(read_workload_sdk_environment=lambda: sdk_identity),
    )
    monkeypatch.setattr(
        updater,
        "_load_workload_resolver",
        lambda: SimpleNamespace(potential_component_ids=lambda *_args: (ECHO_ID, SDK_ID)),
    )
    monkeypatch.setattr(
        updater,
        "_read_workload_package_inventory",
        lambda *_args, **_kwargs: copy.deepcopy(inventory),
    )
    monkeypatch.setattr(updater, "_ensure_workload_echo_api_token", _record_redacted_token)
    monkeypatch.setattr(updater, "_ensure_workload_phase", _record_phase)
    monkeypatch.setattr(updater, "_end_workload_hold", _record_end_phase)
    monkeypatch.setattr(
        updater,
        "_load_workload_web_host",
        lambda: pytest.fail("Echo OCI must not use the static-web adapter"),
    )
    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    helper, calls = _install_host_stubs(updater, monkeypatch)

    catalyst_receipt = (
        updater._installed_component_directory("cyrene-catalyst", create=True) / "owner.json"
    )
    catalyst_receipt.write_text('{"kept":true}\n', encoding="utf-8")
    catalyst_data = tmp_path / "catalyst-data" / "dataset.marker"
    catalyst_data.parent.mkdir()
    catalyst_data.write_text("catalyst-data-retained\n", encoding="utf-8")
    receipt_before = catalyst_receipt.read_bytes()
    data_before = catalyst_data.read_bytes()

    result = updater._apply_workload_install_assembled(stored, {})

    assert result["status"] == "activated"
    assert result["workloadId"] == "echo"
    assert result["sourceBindings"] == inventory["sourceBindings"]
    assert calls["pull"] and calls["start"]
    assert calls["pull"][0][0].image_reference == staged_echo["stagedIdentity"]["imageReference"]
    spec = calls["start"][0][0]
    assert spec.activity_source_id == ECHO_ID
    assert spec.host_uid == 1001 and spec.host_gid == 1001
    assert str(spec.data_directory) == "/var/lib/cyrene/echo"
    assert spec.private_environment == {
        "CYRENE_DATA_TOOLS_TOKEN": TOKEN,
        "CYRENE_EVALUATION_RUNNER_CONNECTION_REF": CONNECTION_REF,
    }
    assert TOKEN not in repr(spec) and CONNECTION_REF not in repr(spec)
    receipt = updater._read_workload_echo_receipt()
    assert receipt is not None
    assert receipt["releaseId"] == echo_selected["releaseId"]
    assert receipt["verification"] == {"identityAttested": True}
    assert receipt["runnerIdentity"] == RUNNER_IDENTITY
    installed, _package_state = updater._installed_workload_components(
        (ECHO_ID,), workload_id="echo"
    )
    assert installed[ECHO_ID]["releaseId"] == echo_selected["releaseId"]
    assert installed[ECHO_ID]["imageDigest"] == echo_selected["digest"]
    assert installed[ECHO_ID]["attestationRef"] == echo_selected["attestationRef"]

    monkeypatch.setattr(updater, "_reload_catalog_for_operation", lambda: None)
    status_resolver = SimpleNamespace(potential_component_ids=lambda *_args: (ECHO_ID, SDK_ID))
    monkeypatch.setattr(updater, "_load_workload_resolver", lambda: status_resolver)
    status = updater.workload_status("echo")
    status_echo = next(row for row in status["components"] if row["componentId"] == ECHO_ID)
    assert status_echo["installed"] is True
    assert status_echo["digest"] == echo_selected["digest"]
    assert status["hostMetadata"]["echo"]["available"] is True

    transaction_path = updater._private_state_directory("transactions") / f"{plan_id}.json"
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    assert transaction["testPhases"] == ["core-runtime-install", "core-runtime-activate"]
    assert transaction["sdkEnvironment"] == sdk_identity
    public_before_uninstall = json.dumps([result, status, receipt], sort_keys=True)
    private_journal = json.dumps(transaction, sort_keys=True)
    assert TOKEN not in public_before_uninstall
    assert CONNECTION_REF not in public_before_uninstall
    assert TOKEN not in private_journal
    assert CONNECTION_REF not in private_journal
    assert catalyst_receipt.read_bytes() == receipt_before
    assert catalyst_data.read_bytes() == data_before

    exact_owner = {
        "componentId": RUNNER_IDENTITY["componentId"],
        "bindingId": RUNNER_IDENTITY["bindingId"],
        "packageId": RUNNER_IDENTITY["packageId"],
    }
    installation_record = {
        "installation_id": RUNNER_IDENTITY["installationId"],
        "package_id": RUNNER_IDENTITY["packageId"],
        "package_version": "0.1.0",
        "archive_digest": _sha("exact-match-archive"),
        "artifact_digest": _sha("exact-match-artifact"),
    }
    catalyst_installation = {
        "installation_id": "catalyst-owner-installation-1",
        "package_id": "cyrene.data-tools",
        "package_version": "1.0.0",
        "archive_digest": _sha("catalyst-archive"),
        "artifact_digest": _sha("catalyst-artifact"),
    }
    inventory["installationRecords"].update(
        {
            exact_owner["componentId"]: installation_record,
            "cyrene-catalyst-data-tools": catalyst_installation,
        }
    )
    inventory["sourceBindings"].append(
        {
            "sourceId": ECHO_ID,
            "bindingId": exact_owner["bindingId"],
            "packageId": exact_owner["packageId"],
            "activeInstallationId": RUNNER_IDENTITY["installationId"],
            "installationIds": [RUNNER_IDENTITY["installationId"]],
            "state": "RUNNING",
        }
    )
    foreign_echo_binding = {
        "sourceId": ECHO_ID,
        "bindingId": "unrecognized-foreign-echo-binding",
        "packageId": "cyrene.tools.dataset.preparation",
        "activeInstallationId": "foreign-echo-installation-1",
        "installationIds": ["foreign-echo-installation-1"],
        "state": "RUNNING",
    }
    inventory["sourceBindings"].append(foreign_echo_binding)
    runtime_status_reads: list[dict[str, Any]] = []
    deactivations: list[dict[str, Any]] = []
    deactivate_attempts = 0

    def read_runner_identity(
        package_state: dict[str, Any], *, prior_identity: dict[str, Any] | None = None
    ) -> tuple[str | None, dict[str, Any] | None]:
        runtime_status_reads.append({"priorIdentity": prior_identity})
        owner_binding = next(
            row
            for row in package_state["sourceBindings"]
            if row.get("sourceId") == ECHO_ID and row.get("bindingId") == exact_owner["bindingId"]
        )
        assert owner_binding["activeInstallationId"] == RUNNER_IDENTITY["installationId"]
        if owner_binding["state"] == "RUNNING":
            return CONNECTION_REF, RUNNER_IDENTITY
        return None, prior_identity

    def deactivate_echo_owner(
        transaction: dict[str, Any],
        _transaction_path: Path,
        *,
        operation: str,
        component: dict[str, Any],
        installation_id: str,
        **_kwargs: Any,
    ) -> None:
        nonlocal deactivate_attempts
        assert operation == "deactivate"
        assert component == exact_owner
        assert installation_id == RUNNER_IDENTITY["installationId"]
        assert transaction.get("echoContainerRemoved") is True
        assert all(
            hold.get("status") == "ended" for hold in transaction["maintenanceHolds"].values()
        )
        deactivate_attempts += 1
        if deactivate_attempts == 1:
            raise updates.UpdateError(
                "PACKAGE_RUNTIME_OPERATION_FAILED",
                "simulated interruption after Echo container removal",
                retryable=True,
            )
        deactivations.append({"component": component, "installationId": installation_id})
        for binding in inventory["sourceBindings"]:
            if (
                binding.get("sourceId") == ECHO_ID
                and binding.get("bindingId") == exact_owner["bindingId"]
            ):
                binding["state"] = "STOPPED"
                binding["activeInstallationId"] = None
        transaction.setdefault("bindingOperations", []).append(
            {"operation": operation, "componentId": component["componentId"], "state": "STOPPED"}
        )

    uninstall_plan_id = "plan-" + "e" * 32
    uninstall_digest = _sha("echo-uninstall-plan")
    uninstall_row = {
        **echo_selected,
        "installedIdentity": {"imageDigest": echo_selected["digest"]},
    }
    uninstall_resolution = {
        "status": "ready",
        "planDigest": uninstall_digest,
        "selectedComponents": [uninstall_row],
        "sourceBindings": copy.deepcopy(inventory["sourceBindings"]),
    }
    uninstall_staged = {"componentId": ECHO_ID, "status": "current"}
    uninstall_stored = {
        "planId": uninstall_plan_id,
        "planDigest": uninstall_digest,
        "workloadId": "echo",
        "targetId": updates.WORKLOAD_HOST_TARGET,
        "catalogDigest": updater.catalog_digest,
        "catalogGeneration": updater.catalog_generation,
        "channel": "preview",
        "action": "uninstall",
        "planKind": updates.WORKLOAD_PROTOCOL_VERSION,
        "phase": "staged",
        "selections": {},
        "stagedComponents": [uninstall_staged],
    }
    updates._atomic_json(
        updater._workload_plan_directory() / f"{uninstall_plan_id}.json", uninstall_stored
    )
    build_count = 0

    def uninstall_plan(
        *_args: Any, **_kwargs: Any
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        nonlocal build_count
        build_count += 1
        current = (
            uninstall_resolution
            if build_count == 1
            else {**uninstall_resolution, "status": "blocked"}
        )
        return current, {}, copy.deepcopy(inventory)

    monkeypatch.setattr(updater, "_build_workload_plan", uninstall_plan)
    monkeypatch.setattr(
        updater,
        "_workload_plugin_owner_rows",
        lambda *_args: (echo_selected["sourcePolicy"], [exact_owner]),
    )
    monkeypatch.setattr(updater, "_workload_echo_runner_identity", read_runner_identity)
    monkeypatch.setattr(
        updater,
        "_workload_source_state",
        lambda *_args: (
            _activity_catalog()[0],
            {"schemaVersion": 1},
            {ECHO_ID: {"uid": 1001, "gid": 1001}},
            object(),
        ),
    )
    monkeypatch.setattr(updater, "_workload_binding_operation", deactivate_echo_owner)
    monkeypatch.setattr(
        updater,
        "_read_workload_package_inventory",
        lambda *_args, **_kwargs: copy.deepcopy(inventory),
    )
    monkeypatch.setattr(
        updater, "_require_workload_target", lambda workload, target: (workload, target)
    )
    remove_attempts = 0
    status_calls_before = len(calls["status"])

    def remove_once_then_succeed(
        spec: Any, *, runner: Any, remove_runtime_credentials: bool
    ) -> Any:
        nonlocal remove_attempts
        remove_attempts += 1
        calls["remove"].append((spec, runner, remove_runtime_credentials))
        echo_binding = next(
            row for row in inventory["sourceBindings"] if row.get("sourceId") == ECHO_ID
        )
        assert echo_binding["state"] == "RUNNING"
        assert remove_runtime_credentials is True
        if remove_attempts == 1:
            raise RuntimeError("simulated interrupted image cleanup")
        return helper.OciContainerObservation(
            component_id=spec.component_id,
            target_id=spec.target_id,
            manifest_digest=spec.manifest_digest,
            image_digest=spec.image_digest,
            image_reference=spec.image_reference,
            container_name=spec.container_name,
            container_id=None,
            state="not-installed",
            host_uid=spec.host_uid,
            host_gid=spec.host_gid,
            activity_source_id=spec.activity_source_id,
            activity_catalog_generation=spec.activity_catalog_generation,
            data_directory=str(spec.data_directory),
            artifact_directory=str(spec.artifact_directory),
        )

    monkeypatch.setattr(helper, "remove_container", remove_once_then_succeed)
    confirmation = {"planId": uninstall_plan_id, "planDigest": uninstall_digest, "confirmed": True}
    with pytest.raises(updates.UpdateError) as interrupted:
        updater._apply_workload_locked(
            "echo",
            updates.WORKLOAD_HOST_TARGET,
            uninstall_plan_id,
            uninstall_digest,
            confirmation,
            action="uninstall",
            channel="preview",
        )
    assert interrupted.value.code == "COMPONENT_IN_USE"
    assert remove_attempts == 0
    inventory["sourceBindings"].remove(foreign_echo_binding)

    with pytest.raises(updates.UpdateError) as interrupted_remove:
        updater._apply_workload_locked(
            "echo",
            updates.WORKLOAD_HOST_TARGET,
            uninstall_plan_id,
            uninstall_digest,
            confirmation,
            action="uninstall",
            channel="preview",
        )
    assert interrupted_remove.value.code == "WORKLOAD_OCI_REMOVE_FAILED"
    uninstall_transaction_path = (
        updater._private_state_directory("transactions") / f"{uninstall_plan_id}.json"
    )
    interrupted_journal = json.loads(uninstall_transaction_path.read_text(encoding="utf-8"))
    assert interrupted_journal["echoUninstallIntent"] is True
    assert len(calls["status"]) == status_calls_before + 1
    assert not deactivations
    assert (
        next(row for row in inventory["sourceBindings"] if row.get("sourceId") == ECHO_ID)["state"]
        == "RUNNING"
    )

    with pytest.raises(updates.UpdateError) as interrupted_after_removal:
        updater._apply_workload_locked(
            "echo",
            updates.WORKLOAD_HOST_TARGET,
            uninstall_plan_id,
            uninstall_digest,
            confirmation,
            action="uninstall",
            channel="preview",
        )
    assert interrupted_after_removal.value.code == "PACKAGE_RUNTIME_OPERATION_FAILED"
    removed_journal = json.loads(uninstall_transaction_path.read_text(encoding="utf-8"))
    assert removed_journal["echoContainerRemoved"] is True
    assert all(
        hold.get("status") == "ended" for hold in removed_journal["maintenanceHolds"].values()
    )
    assert not deactivations
    assert (
        next(row for row in inventory["sourceBindings"] if row.get("sourceId") == ECHO_ID)["state"]
        == "RUNNING"
    )

    result_uninstall = updater._apply_workload_locked(
        "echo",
        updates.WORKLOAD_HOST_TARGET,
        uninstall_plan_id,
        uninstall_digest,
        confirmation,
        action="uninstall",
        channel="preview",
    )

    assert result_uninstall["status"] == "uninstalled"
    assert result_uninstall["sourceBindings"] == inventory["sourceBindings"]
    assert remove_attempts == 2
    assert deactivate_attempts == 2
    assert len(calls["status"]) == status_calls_before + 1
    assert all(call[2] is True for call in calls["remove"])
    assert deactivations == [
        {"component": exact_owner, "installationId": RUNNER_IDENTITY["installationId"]}
    ]
    assert len(runtime_status_reads) >= 2
    assert all(read["priorIdentity"] == RUNNER_IDENTITY for read in runtime_status_reads)
    assert (
        next(row for row in inventory["sourceBindings"] if row.get("sourceId") == ECHO_ID)["state"]
        == "STOPPED"
    )
    assert inventory["installationRecords"][exact_owner["componentId"]] == installation_record
    assert inventory["installationRecords"]["cyrene-catalyst-data-tools"] == catalyst_installation
    assert str(calls["remove"][0][0].data_directory) == "/var/lib/cyrene/echo"
    assert updater._read_workload_echo_receipt() is None
    assert catalyst_receipt.read_bytes() == receipt_before
    assert catalyst_data.read_bytes() == data_before
    final_public = json.dumps(result_uninstall, sort_keys=True)
    assert TOKEN not in final_public
    assert CONNECTION_REF not in final_public
    final_journal = json.loads(uninstall_transaction_path.read_text(encoding="utf-8"))
    final_serialized = json.dumps(final_journal, sort_keys=True)
    assert final_journal["sdkEnvironment"] == sdk_identity
    assert TOKEN not in final_serialized
    assert CONNECTION_REF not in final_serialized


def _record_redacted_token(transaction: dict[str, Any], _transaction_path: Path) -> str:
    """Model only the protected-file receipt; never journal the bearer value."""

    transaction["echoApiTokenIdentity"] = {
        "path": "/etc/cyrene/secrets/catalyst-api-token",
        "sha256": _sha(TOKEN),
    }
    return TOKEN


def _record_phase(
    transaction: dict[str, Any],
    _transaction_path: Path,
    *,
    phase: str,
    target_kind: str,
    requires_restart: bool,
) -> bool:
    """Record the requested transaction phase without acquiring a real broker hold."""

    del requires_restart
    holds = transaction.setdefault("maintenanceHolds", {})
    prior = holds.get(phase)
    if isinstance(prior, dict) and prior.get("status") == "ended":
        return False
    transaction["maintenancePhase"] = phase
    holds[phase] = {"status": "active", "targetKind": target_kind}
    transaction.setdefault("testPhases", []).append(phase)
    updates._atomic_json(_transaction_path, transaction)
    return True


def _record_end_phase(
    transaction: dict[str, Any],
    transaction_path: Path,
    *,
    outcome: str,
    healthy: bool,
) -> None:
    """Close the in-memory test hold before exercising owner-scoped SDK calls."""

    phase = transaction["maintenancePhase"]
    hold = transaction["maintenanceHolds"][phase]
    hold.update({"status": "ended", "outcome": outcome, "healthy": healthy})
    updates._atomic_json(transaction_path, transaction)
