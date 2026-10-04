"""Regression tests for the trusted local paths in Product bundle manifests."""

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
SPEC = importlib.util.spec_from_file_location("data_bundle_inner_manifest", UPDATER_PATH)
assert SPEC is not None and SPEC.loader is not None
updater_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = updater_module
SPEC.loader.exec_module(updater_module)


def _hex_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fixture(root: Path) -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    trusted_owners = {owner["ownerId"]: owner for owner in catalog["dataBundleTrust"]["owners"]}
    payload_root = root / "payload"
    payload_root.mkdir()
    proof_owners: list[dict[str, Any]] = []
    manifest_owners: list[dict[str, str]] = []
    files: list[dict[str, str]] = []

    for index, (owner_id, trusted) in enumerate(sorted(trusted_owners.items()), start=1):
        repository = trusted["repository"]
        local_repository = repository.rsplit("/", 1)[-1]
        catalog_path = f"{local_repository}/{trusted['catalogPath']}"
        commit = f"{index:040x}"
        catalog_bytes = f"catalog for {owner_id}\n".encode()
        catalog_digest = _hex_digest(catalog_bytes)
        catalog_file = payload_root / catalog_path
        catalog_file.parent.mkdir(parents=True, exist_ok=True)
        catalog_file.write_bytes(catalog_bytes)
        proof_owners.append(
            {
                "ownerId": owner_id,
                "source": {"repository": repository, "commit": commit},
                "catalogSha256": f"sha256:{catalog_digest}",
            }
        )
        manifest_owners.append(
            {
                "ownerId": owner_id,
                "repository": local_repository,
                "sourceSha": commit,
                "catalogPath": catalog_path,
                "catalogSha256": catalog_digest,
            }
        )
        files.append({"path": catalog_path, "sha256": catalog_digest})

    extra_path = "Cyrene-Catalyst/contracts/product/v1/openapi.yaml"
    extra_bytes = b"openapi: 3.1.0\npaths: {}\n"
    extra_file = payload_root / extra_path
    extra_file.parent.mkdir(parents=True, exist_ok=True)
    extra_file.write_bytes(extra_bytes)
    files.append({"path": extra_path, "sha256": _hex_digest(extra_bytes)})

    policy_bytes = b'{"schemaVersion":"cyrene.workspace.product.authorization-policy.v2"}\n'
    policy_path = payload_root / "workspace-product-policy-v2.json"
    policy_path.write_bytes(policy_bytes)
    proof = {
        "owners": proof_owners,
        "policySha256": f"sha256:{_hex_digest(policy_bytes)}",
    }
    manifest = {
        "formatVersion": 2,
        "wireApiVersion": catalog["dataBundleTrust"]["protocolVersion"],
        "owners": manifest_owners,
        "files": [*files, {"path": policy_path.name, "sha256": _hex_digest(policy_bytes)}],
    }
    updater = object.__new__(updater_module.ComponentUpdater)
    return (
        updater,
        manifest,
        proof,
        {
            "payloadRoot": payload_root,
            "trustedOwners": trusted_owners,
            "extraPath": extra_path,
        },
    )


def _validate(
    updater: Any,
    manifest: dict[str, Any],
    proof: dict[str, Any],
    payload_root: Path,
    trusted_owners: dict[str, dict[str, Any]],
) -> None:
    updater._validate_inner_bundle_files(manifest, proof, payload_root, {}, trusted_owners)


def test_platform_short_repository_paths_match_trusted_full_sources(tmp_path: Path) -> None:
    updater, manifest, proof, context = _fixture(tmp_path)

    _validate(
        updater,
        manifest,
        proof,
        context["payloadRoot"],
        context["trustedOwners"],
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "canonical_repository",
        "untrusted_alias",
        "canonical_path",
        "wrong_path",
        "source_sha",
        "catalog_digest",
    ],
)
def test_inner_owner_must_match_trusted_short_projection(tmp_path: Path, mutation: str) -> None:
    updater, manifest, proof, context = _fixture(tmp_path)
    owner = manifest["owners"][0]
    trusted = context["trustedOwners"][owner["ownerId"]]

    if mutation == "canonical_repository":
        owner["repository"] = trusted["repository"]
    elif mutation == "untrusted_alias":
        owner["repository"] = "Cyrene-Attacker"
    elif mutation == "canonical_path":
        owner["catalogPath"] = f"{trusted['repository']}/{trusted['catalogPath']}"
    elif mutation == "wrong_path":
        owner["catalogPath"] = f"{owner['repository']}/contracts/product/v2/other.json"
    elif mutation == "source_sha":
        owner["sourceSha"] = "f" * 40
    else:
        owner["catalogSha256"] = "f" * 64

    with pytest.raises(updater_module.UpdateError) as error:
        _validate(
            updater,
            manifest,
            proof,
            context["payloadRoot"],
            context["trustedOwners"],
        )

    assert error.value.code == "DATA_BUNDLE_MANIFEST_MISMATCH"


def test_all_inner_file_hashes_are_checked(tmp_path: Path) -> None:
    updater, manifest, proof, context = _fixture(tmp_path)
    (context["payloadRoot"] / context["extraPath"]).write_bytes(b"changed after manifest")

    with pytest.raises(updater_module.UpdateError) as error:
        _validate(
            updater,
            manifest,
            proof,
            context["payloadRoot"],
            context["trustedOwners"],
        )

    assert error.value.code == "DATA_BUNDLE_FILE_MISMATCH"


@pytest.mark.parametrize("unsafe_path", ["../outside.json", "Cyrene-Catalyst/../outside.json"])
def test_inner_file_path_must_remain_safe(tmp_path: Path, unsafe_path: str) -> None:
    updater, manifest, proof, context = _fixture(tmp_path)
    manifest["files"].append({"path": unsafe_path, "sha256": "a" * 64})

    with pytest.raises(updater_module.UpdateError) as error:
        _validate(
            updater,
            manifest,
            proof,
            context["payloadRoot"],
            context["trustedOwners"],
        )

    assert error.value.code == "INVALID_MANIFEST"


def test_inner_file_symlinks_are_rejected(tmp_path: Path) -> None:
    updater, manifest, proof, context = _fixture(tmp_path)
    symlink_path = "Cyrene-Catalyst/contracts/product/v1/symlink.openapi.yaml"
    target = context["payloadRoot"] / "outside.openapi.yaml"
    target.write_bytes(b"outside payload")
    (context["payloadRoot"] / symlink_path).symlink_to(target)
    manifest["files"].append({"path": symlink_path, "sha256": _hex_digest(target.read_bytes())})

    with pytest.raises(updater_module.UpdateError) as error:
        _validate(
            updater,
            manifest,
            proof,
            context["payloadRoot"],
            context["trustedOwners"],
        )

    assert error.value.code == "DATA_BUNDLE_FILE_MISMATCH"
