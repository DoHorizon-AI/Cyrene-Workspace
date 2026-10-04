"""Regression tests for source-kind-specific data-bundle attestations."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
UPDATER_PATH = WORKSPACE_ROOT / "packaging/component_updates.py"
CATALOG_PATH = WORKSPACE_ROOT / "packaging/component-catalog-bootstrap-v1.json"
SPEC = importlib.util.spec_from_file_location("data_bundle_source_workflows", UPDATER_PATH)
assert SPEC is not None and SPEC.loader is not None
updater_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = updater_module
SPEC.loader.exec_module(updater_module)

OWNER_WORKFLOW_SUFFIX = "/.github/workflows/product-contract.yml"
POLICY_WORKFLOW_SUFFIX = "/.github/workflows/product-policy-release.yml"
NATIVE_WORKFLOW_SUFFIX = "/.github/workflows/component-release.yml"
ATTACKER_WORKFLOW = "attacker/repository/.github/workflows/trusted.yml"
PROTOCOL_VERSION = "cyrene.workspace.product.v2"
POLICY_SCHEMA_VERSION = "cyrene.workspace.product.authorization-policy.v2"


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _attestation(repository: str, workflow: str, subject_name: str, commit: str) -> dict[str, Any]:
    run_id = "123456"
    attempt = 1
    return {
        "attestation": {
            "kind": "github-artifact-attestation",
            "subjectName": subject_name,
            "repository": repository,
            "workflow": workflow,
            "predicateType": "https://slsa.dev/provenance/v1",
            "run": {
                "id": run_id,
                "attempt": attempt,
                "url": f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{attempt}",
            },
        }
    }


def _source_record(
    root: Path,
    *,
    repository: str,
    source_path: str,
    bundle_path: str,
    subject_name: str,
    workflow: str,
    commit: str,
    subject_bytes: bytes,
) -> dict[str, Any]:
    subject = root / source_path
    subject.parent.mkdir(parents=True, exist_ok=True)
    subject.write_bytes(subject_bytes)
    bundle = root / bundle_path
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_bytes(b"fixed detached attestation fixture\n")
    subject_digest = _digest(subject_bytes)
    return {
        "source": {
            "repository": repository,
            "ref": "refs/heads/develop",
            "commit": commit,
        },
        "catalogPath": source_path,
        "path": source_path,
        "catalogSha256": subject_digest,
        "sha256": subject_digest,
        "provenance": {
            "attestation": _attestation(repository, workflow, subject_name, commit),
            "bundlePath": bundle_path,
            "bundleSha256": _digest(bundle.read_bytes()),
            "subjectPath": source_path,
            "subjectDigest": subject_digest,
        },
    }


def _fixture(
    tmp_path: Path,
) -> tuple[Any, Any, Path, dict[str, Any], list[str], list[dict[str, Any]]]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    trust = catalog["dataBundleTrust"]
    root = tmp_path / "payload"
    root.mkdir()
    records: list[dict[str, Any]] = []
    expected_workflows: list[str] = []
    inner_owners: list[dict[str, str]] = []
    inner_files: list[dict[str, str]] = []

    for index, trusted in enumerate(trust["owners"]):
        owner_id = trusted["ownerId"]
        repository = trusted["repository"]
        commit = f"{index + 1:040x}"
        source_path = f"{repository}/{trusted['catalogPath']}"
        subject_bytes = f"catalog for {owner_id}\n".encode()
        record = _source_record(
            root,
            repository=repository,
            source_path=source_path,
            bundle_path=f"attestations/owners/{owner_id}.jsonl",
            subject_name="catalog.json",
            workflow=repository + OWNER_WORKFLOW_SUFFIX,
            commit=commit,
            subject_bytes=subject_bytes,
        )
        del record["path"]
        del record["sha256"]
        record["ownerId"] = owner_id
        records.append(record)
        expected_workflows.append(repository + OWNER_WORKFLOW_SUFFIX)
        inner_owners.append(
            {
                "ownerId": owner_id,
                "repository": repository,
                "sourceSha": commit,
                "catalogPath": source_path,
                "catalogSha256": record["catalogSha256"].removeprefix("sha256:"),
            }
        )
        inner_files.append(
            {
                "path": source_path,
                "sha256": record["catalogSha256"].removeprefix("sha256:"),
            }
        )

    policy_trust = trust["policySource"]
    policy_repository = policy_trust["repository"]
    policy_commit = "7" * 40
    policy_bytes = json.dumps(
        {"schemaVersion": policy_trust["schemaVersion"]}, sort_keys=True
    ).encode()
    policy_record = _source_record(
        root,
        repository=policy_repository,
        source_path=f"{policy_repository}/{policy_trust['path']}",
        bundle_path="attestations/platform-policy.jsonl",
        subject_name="workspace-product-policy-v2.json",
        workflow=policy_repository + POLICY_WORKFLOW_SUFFIX,
        commit=policy_commit,
        subject_bytes=policy_bytes,
    )
    policy_record.pop("catalogPath")
    policy_record.pop("catalogSha256")
    policy_record["path"] = policy_trust["path"]
    policy_record["sha256"] = _digest(policy_bytes)
    expected_workflows.append(policy_repository + POLICY_WORKFLOW_SUFFIX)

    policy_bundle_path = root / "workspace-product-policy-v2.json"
    policy_bundle_path.write_bytes(policy_bytes)
    inner_files.append(
        {
            "path": "workspace-product-policy-v2.json",
            "sha256": policy_record["sha256"].removeprefix("sha256:"),
        }
    )
    inner_manifest = {
        "formatVersion": 2,
        "wireApiVersion": trust["protocolVersion"],
        "owners": inner_owners,
        "files": inner_files,
    }
    inner_manifest_bytes = (
        json.dumps(inner_manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    (root / trust["manifestPath"]).write_bytes(inner_manifest_bytes)

    proof = {
        "schemaVersion": 1,
        "protocolVersion": trust["protocolVersion"],
        "contractApiVersion": trust["contractApiVersion"],
        "manifestPath": trust["manifestPath"],
        "manifestSha256": _digest(inner_manifest_bytes),
        "policyPath": "workspace-product-policy-v2.json",
        "policySha256": policy_record["sha256"],
        "policySchemaVersion": policy_trust["schemaVersion"],
        "owners": records,
        "policySource": policy_record,
    }
    proof_path = root / "data-bundle-proof-v1.json"
    proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")

    updater = object.__new__(updater_module.ComponentUpdater)
    updater.catalog = catalog
    updater.publishers = {row["repository"]: row for row in catalog["publishers"]}
    verified_calls: list[dict[str, Any]] = []
    updater._verify_attestation = lambda _payload, **kwargs: verified_calls.append(kwargs)
    candidate = updater_module.Candidate(
        component={"componentId": "cyrene-product-contract-bundle"},
        manifest={
            "protocolVersion": PROTOCOL_VERSION,
            "dataBundle": {
                "proofPath": "data-bundle-proof-v1.json",
                "proofSha256": _digest(proof_path.read_bytes()),
            },
        },
        manifest_digest=_digest(b"outer manifest"),
        artifact_digest=_digest(b"archive"),
        manifest_uri="local fixture",
        index={},
        index_uri="local fixture",
    )
    return updater, candidate, root, proof, expected_workflows, verified_calls


def test_owner_and_policy_proofs_use_their_canonical_workflows(tmp_path: Path) -> None:
    updater, candidate, root, _proof, expected_workflows, verified_calls = _fixture(tmp_path)

    updater._validate_data_bundle_proof(candidate, root, "preview")

    assert [call["workflow"] for call in verified_calls] == expected_workflows
    assert len(verified_calls) == 7


@pytest.mark.parametrize("source_kind", ["owner", "policy"])
@pytest.mark.parametrize("workflow", ["native", "attacker"])
def test_untrusted_source_workflow_is_rejected(
    tmp_path: Path, source_kind: str, workflow: str
) -> None:
    updater, candidate, root, proof, _expected_workflows, verified_calls = _fixture(tmp_path)
    if source_kind == "owner":
        record = proof["owners"][0]
        repository = record["source"]["repository"]
    else:
        record = proof["policySource"]
        repository = record["source"]["repository"]
    attestation = record["provenance"]["attestation"]["attestation"]
    attestation["workflow"] = (
        repository + NATIVE_WORKFLOW_SUFFIX if workflow == "native" else ATTACKER_WORKFLOW
    )
    proof_path = root / "data-bundle-proof-v1.json"
    proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
    candidate.manifest["dataBundle"]["proofSha256"] = _digest(proof_path.read_bytes())

    with pytest.raises(updater_module.UpdateError) as error:
        updater._validate_data_bundle_proof(candidate, root, "preview")

    assert error.value.code == "UNTRUSTED_DATA_BUNDLE_ATTESTATION"
    if source_kind == "owner":
        assert verified_calls == []
    else:
        assert len(verified_calls) == 6
        assert all(call["workflow"].endswith(OWNER_WORKFLOW_SUFFIX) for call in verified_calls)


@pytest.mark.parametrize("source_kind", ["owner", "policy"])
def test_provenance_subject_path_must_match_trusted_source_mapping(
    tmp_path: Path, source_kind: str
) -> None:
    updater, candidate, root, proof, _expected_workflows, verified_calls = _fixture(tmp_path)
    record = proof["owners"][0] if source_kind == "owner" else proof["policySource"]
    record["provenance"]["subjectPath"] = "attacker/repository/untrusted.json"
    proof_path = root / "data-bundle-proof-v1.json"
    proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
    candidate.manifest["dataBundle"]["proofSha256"] = _digest(proof_path.read_bytes())

    with pytest.raises(updater_module.UpdateError) as error:
        updater._validate_data_bundle_proof(candidate, root, "preview")

    assert error.value.code == "DATA_BUNDLE_SUBJECT_MISMATCH"
    if source_kind == "owner":
        assert verified_calls == []
    else:
        assert len(verified_calls) == 6
        assert all(call["workflow"].endswith(OWNER_WORKFLOW_SUFFIX) for call in verified_calls)
