#!/usr/bin/env python3
"""Package an independently attested Product contract data bundle release.

The caller supplies immutable owner and policy publication assets that were
verified by ``gh attestation verify``. This tool preserves those proofs and
never creates an aggregate signature in their place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import rfc8785
import zstandard
from jsonschema import Draft202012Validator
from referencing import Registry, Resource


REPOSITORY = "DoHorizon-AI/Cyrene-Workspace"
COMPONENT_ID = "cyrene-product-contract-bundle"
TARGET_ID = "portable-contract-data-v1"
PROTOCOL = "cyrene.workspace.product.v2"
OWNER_IDS = ("catalyst", "echo", "exchange", "navigator", "reactor", "yield")
OWNER_REPOSITORIES = {
    owner_id: f"DoHorizon-AI/Cyrene-{owner_id.title()}" for owner_id in OWNER_IDS
}
POLICY_REPOSITORY = "DoHorizon-AI/Cyrene-Platform"
POLICY_SOURCE_PATH = "contracts/policies/workspace-product-policy-v2.json"
POLICY_BUNDLE_PATH = "workspace-product-policy-v2.json"
PROOF_NAME = "data-bundle-proof-v1.json"
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_FILES = 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


class ReleaseError(ValueError):
    """Raised when a pinned release input is incomplete or inconsistent."""


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _jcs_digest(document: dict[str, Any], field: str) -> str:
    unsigned = {key: value for key, value in document.items() if key != field}
    return _digest(rfc8785.dumps(unsigned))


def _read_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError(f"invalid JSON input: {path}") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"JSON root must be an object: {path}")
    return value


def _regular_file(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ReleaseError(f"input must be a regular non-symlink file: {path}")
    return path.read_bytes()


def _check_attestation_metadata(
    wrapper: Any,
    *,
    repository: str,
    workflow: str,
    subject_name: str,
    ref: str,
    commit: str,
) -> dict[str, Any]:
    if not isinstance(wrapper, dict) or set(wrapper) != {"attestation"}:
        raise ReleaseError("publisher attestation metadata must use the fixed v1 wrapper")
    identity = wrapper["attestation"]
    if not isinstance(identity, dict):
        raise ReleaseError("publisher attestation identity must be an object")
    run = identity.get("run")
    if (
        identity.get("kind") != "github-artifact-attestation"
        or identity.get("subjectName") != subject_name
        or identity.get("repository") != repository
        or identity.get("workflow") != workflow
        or identity.get("predicateType") != "https://slsa.dev/provenance/v1"
        or not isinstance(run, dict)
        or not isinstance(run.get("id"), str)
        or not run["id"].isdigit()
        or not isinstance(run.get("attempt"), int)
        or run["attempt"] < 1
        or run.get("url") != f"https://github.com/{repository}/actions/runs/{run.get('id')}/attempts/{run.get('attempt')}"
        or not ref.startswith("refs/heads/")
        or not GIT_SHA.fullmatch(commit)
    ):
        raise ReleaseError("publisher attestation identity does not match the trusted source/workflow")
    return wrapper


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ReleaseError(f"unsafe archive path: {value!r}")
    return path


def _file_map(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    expanded_bytes = 0
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(current)
        for name in (*directory_names, *file_names):
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ReleaseError(f"symlinks are forbidden in the staged bundle: {path}")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ReleaseError(f"special files are forbidden in the staged bundle: {path}")
            if info.st_size > MAX_MEMBER_BYTES:
                raise ReleaseError(f"staged bundle file exceeds {MAX_MEMBER_BYTES} bytes: {path}")
            expanded_bytes += info.st_size
            if expanded_bytes > MAX_EXPANDED_BYTES:
                raise ReleaseError(f"staged bundle exceeds {MAX_EXPANDED_BYTES} expanded bytes")
            relative = path.relative_to(root).as_posix()
            _safe_relative(relative)
            result[relative] = _digest(path.read_bytes())
            if len(result) > MAX_FILES:
                raise ReleaseError(f"staged bundle exceeds {MAX_FILES} files")
    if not result:
        raise ReleaseError("staged bundle is empty")
    return dict(sorted(result.items()))


def _write_tar_zst(root: Path, destination: Path) -> None:
    """Write a deterministic tar.zst with normalized ownership and timestamps."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    compressor = zstandard.ZstdCompressor(level=19, threads=0, write_checksum=True, write_content_size=True)
    with destination.open("wb") as output, compressor.stream_writer(output, closefd=False) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for relative in _file_map(root):
                source = root / relative
                source_info = source.stat()
                entry = tarfile.TarInfo(relative)
                entry.size = source_info.st_size
                entry.mtime = 0
                entry.uid = 0
                entry.gid = 0
                entry.uname = ""
                entry.gname = ""
                entry.mode = 0o755 if source_info.st_mode & 0o111 else 0o644
                with source.open("rb") as stream:
                    archive.addfile(entry, stream)


def _parse_pairs(values: list[str], label: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for item in values:
        key, separator, raw_path = item.partition("=")
        if not separator or not key or not raw_path or key in result:
            raise ReleaseError(f"{label} entries must be unique NAME=PATH pairs")
        result[key] = Path(raw_path)
    return result


def _catalog_policy(catalog_path: Path, repository: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    catalog = _read_json(catalog_path)
    if catalog.get("schemaVersion") != 1:
        raise ReleaseError("component catalog schemaVersion must be 1")
    publisher = next(
        (row for row in catalog.get("publishers", []) if row.get("repository") == repository), None
    )
    component = next(
        (row for row in catalog.get("components", []) if row.get("componentId") == COMPONENT_ID), None
    )
    group = next(
        (row for row in catalog.get("compatibilityGroups", []) if row.get("groupId") == "workspace-product-v2"),
        None,
    )
    if not isinstance(publisher, dict) or publisher.get("workflow") != f"{repository}/.github/workflows/data-bundle-release.yml":
        raise ReleaseError("catalog does not authorize this data-bundle workflow")
    if not isinstance(component, dict) or component.get("publisher") != repository:
        raise ReleaseError("catalog does not authorize this data-bundle component publisher")
    if not isinstance(group, dict) or group.get("groupVersion") != "2":
        raise ReleaseError("catalog does not pin workspace-product-v2 groupVersion 2")
    protocol_lock = group.get("contractLock")
    if (
        not isinstance(protocol_lock, dict)
        or protocol_lock.get("repository") != "DoHorizon-AI/Cyrene-Workspace"
        or protocol_lock.get("path") != "governance/workspace-connection-protocols-v2.lock.json"
        or not isinstance(protocol_lock.get("commit"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", protocol_lock["commit"])
        or not isinstance(protocol_lock.get("sha256"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", protocol_lock["sha256"])
    ):
        raise ReleaseError("catalog does not pin the immutable Workspace connection protocol lock")
    try:
        protocol_bytes = subprocess.run(
            ["git", "show", f"{protocol_lock['commit']}:{protocol_lock['path']}"],
            cwd=catalog_path.resolve().parent.parent,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseError("cannot read the exact pinned Workspace connection protocol lock") from error
    if _digest(protocol_bytes) != protocol_lock["sha256"]:
        raise ReleaseError("connection protocol lock bytes do not match the trusted catalog pin")
    if component.get("protocolVersion") != PROTOCOL:
        raise ReleaseError("catalog does not pin the Product v2 protocol")
    if component.get("restart") != {"group": "none"}:
        raise ReleaseError("data bundle must not declare a process restart")
    supported = any(
        isinstance(row, dict)
        and row.get("targetId") == TARGET_ID
        and row.get("artifactKind") == "data-bundle"
        and row.get("support") == "supported"
        for row in component.get("targets", [])
    )
    if not supported:
        raise ReleaseError("catalog does not support portable contract data target")
    trust = catalog.get("dataBundleTrust")
    trusted_owners = trust.get("owners") if isinstance(trust, dict) else None
    trusted_owner_map = {
        owner.get("ownerId"): owner
        for owner in trusted_owners or []
        if isinstance(owner, dict)
    }
    if set(trusted_owner_map) != set(OWNER_IDS):
        raise ReleaseError("catalog dataBundleTrust does not contain the exact six trusted owners")
    for owner_id in OWNER_IDS:
        owner = trusted_owner_map[owner_id]
        if (
            owner.get("repository") != OWNER_REPOSITORIES[owner_id]
            or owner.get("catalogPath") != "contracts/product/v2/catalog.json"
        ):
            raise ReleaseError(f"catalog dataBundleTrust owner mapping is not trusted for {owner_id}")
    policy_source = trust.get("policySource") if isinstance(trust, dict) else None
    if (
        not isinstance(policy_source, dict)
        or policy_source.get("repository") != POLICY_REPOSITORY
        or policy_source.get("path") != POLICY_SOURCE_PATH
        or policy_source.get("schemaVersion") != "cyrene.workspace.product.authorization-policy.v2"
        or trust.get("manifestPath") != "product-contract-bundle.json"
        or trust.get("protocolVersion") != PROTOCOL
        or trust.get("contractApiVersion") != group.get("contractApiVersion")
    ):
        raise ReleaseError("catalog dataBundleTrust policy/bundle pins are not trusted")
    return catalog, component, group


def _owner_entry(
    owner_id: str, publication: dict[str, Any], asset_dir: Path, channel: str, expected_commit: str
) -> dict[str, Any]:
    repository = OWNER_REPOSITORIES[owner_id]
    source = publication.get("source")
    if not isinstance(source, dict) or source.get("repository") != repository:
        raise ReleaseError(f"{owner_id}: owner publication source repository is not trusted")
    ref = source.get("ref")
    commit = source.get("commit")
    allowed_refs = {"preview": "refs/heads/develop", "stable": ("refs/heads/main", "refs/heads/release")}
    if isinstance(allowed_refs[channel], str):
        valid_ref = ref == allowed_refs[channel]
    else:
        valid_ref = ref in allowed_refs[channel]
    if not valid_ref or not isinstance(commit, str) or not GIT_SHA.fullmatch(commit) or commit != expected_commit:
        raise ReleaseError(f"{owner_id}: source ref/commit is not an immutable channel pin")
    catalog_path = f"{repository}/contracts/product/v2/catalog.json"
    if publication.get("ownerId") != owner_id or publication.get("catalogPath") != catalog_path:
        raise ReleaseError(f"{owner_id}: publication owner/catalog path mismatch")
    catalog_raw = _regular_file(asset_dir / "product-catalog-v2.json")
    provenance = publication.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("subjectPath") != catalog_path:
        raise ReleaseError(f"{owner_id}: publication attested subject path mismatch")
    catalog_digest = _digest(catalog_raw)
    if publication.get("catalogSha256") != catalog_digest or provenance.get("subjectDigest") != catalog_digest:
        raise ReleaseError(f"{owner_id}: catalog bytes differ from publisher proof")
    bundle_path = f"attestations/owners/{owner_id}.jsonl"
    bundle_raw = _regular_file(asset_dir / "product-catalog-v2.attestation.jsonl")
    if provenance.get("bundlePath") != bundle_path or provenance.get("bundleSha256") != _digest(bundle_raw):
        raise ReleaseError(f"{owner_id}: attestation bundle differs from publisher proof")
    attestation = _check_attestation_metadata(
        provenance.get("attestation"),
        repository=repository,
        workflow=f"{repository}/.github/workflows/product-contract.yml",
        subject_name="catalog.json",
        ref=ref,
        commit=commit,
    )
    staged_catalog = Path(catalog_path)
    staged_bundle = Path(bundle_path)
    return {
        "ownerId": owner_id,
        "source": {"repository": repository, "ref": ref, "commit": commit},
        "catalogPath": catalog_path,
        "catalogSha256": catalog_digest,
        "provenance": {
            "attestation": attestation,
            "bundlePath": bundle_path,
            "bundleSha256": _digest(bundle_raw),
            "subjectPath": catalog_path,
            "subjectDigest": catalog_digest,
        },
        "_staged": ((staged_catalog, catalog_raw), (staged_bundle, bundle_raw)),
    }


def _policy_entry(
    publication_path: Path, asset_dir: Path, channel: str, expected_commit: str
) -> tuple[dict[str, Any], tuple[tuple[PurePosixPath, bytes], ...]]:
    publication = _read_json(publication_path)
    source = publication.get("source")
    if not isinstance(source, dict) or source.get("repository") != POLICY_REPOSITORY:
        raise ReleaseError("policy publication source repository is not trusted")
    ref = source.get("ref")
    commit = source.get("commit")
    allowed_refs = {"preview": "refs/heads/develop", "stable": ("refs/heads/main", "refs/heads/release")}
    refs = (allowed_refs[channel],) if isinstance(allowed_refs[channel], str) else allowed_refs[channel]
    if ref not in refs or not isinstance(commit, str) or not GIT_SHA.fullmatch(commit) or commit != expected_commit:
        raise ReleaseError("policy source ref/commit is not an immutable channel pin")
    policy_raw = _regular_file(asset_dir / "workspace-product-policy-v2.json")
    bundle_raw = _regular_file(asset_dir / "workspace-product-policy-v2.attestation.jsonl")
    expected = {
        "path": POLICY_BUNDLE_PATH,
        "sha256": _digest(policy_raw),
    }
    if publication.get("policy") != expected:
        raise ReleaseError("policy bytes do not match immutable policy publication metadata")
    provenance = publication.get("provenance")
    if not isinstance(provenance, dict):
        raise ReleaseError("policy publication is missing provenance metadata")
    bundle_path = "attestations/platform-policy.jsonl"
    if provenance.get("bundlePath") != bundle_path or provenance.get("bundleSha256") != _digest(bundle_raw):
        raise ReleaseError("policy attestation bundle differs from publication metadata")
    subject_path = f"{POLICY_REPOSITORY}/{POLICY_SOURCE_PATH}"
    if provenance.get("subjectPath") != subject_path or provenance.get("subjectDigest") != _digest(policy_raw):
        raise ReleaseError("policy attested subject differs from exact policy file")
    attestation = _check_attestation_metadata(
        provenance.get("attestation"),
        repository=POLICY_REPOSITORY,
        workflow=f"{POLICY_REPOSITORY}/.github/workflows/product-policy-release.yml",
        subject_name="workspace-product-policy-v2.json",
        ref=ref,
        commit=commit,
    )
    proof = {
        "source": {"repository": POLICY_REPOSITORY, "ref": ref, "commit": commit},
        "path": POLICY_BUNDLE_PATH,
        "sha256": _digest(policy_raw),
        "provenance": {
            "attestation": attestation,
            "bundlePath": bundle_path,
            "bundleSha256": _digest(bundle_raw),
            "subjectPath": subject_path,
            "subjectDigest": _digest(policy_raw),
        },
    }
    return proof, ((PurePosixPath(POLICY_BUNDLE_PATH), policy_raw), (PurePosixPath(bundle_path), bundle_raw))


def package(args: argparse.Namespace) -> None:
    """Validate the fixed trust map and emit archive, manifest, and index."""
    if not GIT_SHA.fullmatch(args.source_commit):
        raise ReleaseError("Workspace source commit must be a full lowercase Git SHA")
    allowed_refs = {"preview": {"refs/heads/develop"}, "stable": {"refs/heads/main", "refs/heads/release"}}
    if args.source_ref not in allowed_refs[args.channel]:
        raise ReleaseError("Workspace source ref does not match the selected release channel")
    if not args.run_id.isdigit() or int(args.run_id) < 1 or args.run_attempt < 1:
        raise ReleaseError("GitHub Actions run identity is invalid")
    catalog, component, group = _catalog_policy(args.catalog, args.repository)
    owner_publications = _parse_pairs(args.owner_publication, "owner-publication")
    owner_assets = _parse_pairs(args.owner_assets, "owner-assets")
    owner_commits = _parse_pairs(args.owner_commit, "owner-commit")
    if set(owner_publications) != set(OWNER_IDS) or set(owner_assets) != set(OWNER_IDS) or set(owner_commits) != set(OWNER_IDS):
        raise ReleaseError("exactly the six trusted owner proofs and asset directories are required")
    if args.bundle_root.is_symlink() or not args.bundle_root.is_dir():
        raise ReleaseError("generated Product contract bundle root is missing or unsafe")
    if (args.bundle_root / "product-contract-bundle.json").is_file() is False:
        raise ReleaseError("Product v2 builder output lacks its manifest")
    manifest_raw = _regular_file(args.bundle_root / "product-contract-bundle.json")
    manifest_digest = _digest(manifest_raw)
    manifest = _read_json(args.bundle_root / "product-contract-bundle.json")
    if manifest.get("wireApiVersion") != PROTOCOL:
        raise ReleaseError("generated Product manifest does not match protocolVersion")
    policy_proof, policy_files = _policy_entry(
        args.policy_publication, args.policy_assets, args.channel, args.policy_commit
    )
    policy_path = policy_proof["path"]
    policy_bytes = next(data for path, data in policy_files if str(path) == policy_path)
    bundled_policy_path = args.bundle_root / policy_path
    if _regular_file(bundled_policy_path) != policy_bytes:
        raise ReleaseError("generated Product bundle policy differs from the immutable Platform policy asset")

    owners: list[dict[str, Any]] = []
    staged_materials: list[tuple[PurePosixPath, bytes]] = []
    for owner_id in OWNER_IDS:
        publication = _read_json(owner_publications[owner_id])
        commit = str(owner_commits[owner_id])
        if not GIT_SHA.fullmatch(commit):
            raise ReleaseError(f"{owner_id}: owner commit input must be a full lowercase Git SHA")
        owner = _owner_entry(owner_id, publication, owner_assets[owner_id], args.channel, commit)
        owners.append({key: value for key, value in owner.items() if key != "_staged"})
        staged_materials.extend(owner["_staged"])

    proof: dict[str, Any] = {
        "schemaVersion": 1,
        "protocolVersion": PROTOCOL,
        "contractApiVersion": group["contractApiVersion"],
        "manifestPath": "product-contract-bundle.json",
        "manifestSha256": manifest_digest,
        "policyPath": policy_path,
        "policySha256": policy_proof["sha256"],
        "policySchemaVersion": catalog["dataBundleTrust"]["policySource"]["schemaVersion"],
        "owners": owners,
        "policySource": policy_proof,
    }
    output = args.output.resolve()
    if output.exists():
        raise ReleaseError("output directory already exists")
    output.mkdir(parents=True)
    stage = output / "payload"
    shutil.copytree(args.bundle_root, stage, symlinks=False)
    for relative, raw in staged_materials:
        target = stage.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    for relative, raw in policy_files:
        target = stage.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    proof_raw = (json.dumps(proof, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    (stage / PROOF_NAME).write_bytes(proof_raw)
    files = _file_map(stage)
    archive_path = output / f"{COMPONENT_ID}-{args.channel}-{args.source_commit}.tar.zst"
    _write_tar_zst(stage, archive_path)
    archive_raw = archive_path.read_bytes()
    if len(archive_raw) > MAX_ARCHIVE_BYTES:
        raise ReleaseError(f"compressed data bundle exceeds {MAX_ARCHIVE_BYTES} bytes")
    archive_digest = _digest(archive_raw)
    version = args.version
    release_id = f"{args.channel}-{args.source_commit}"
    artifact_name = archive_path.name
    release_uri = f"https://github.com/{args.repository}/releases/download/{release_id}/{artifact_name}"
    run_url = f"https://github.com/{args.repository}/actions/runs/{args.run_id}/attempts/{args.run_attempt}"
    manifest_doc: dict[str, Any] = {
        "schemaVersion": 2,
        "releaseId": release_id,
        "componentId": COMPONENT_ID,
        "version": version,
        "channel": args.channel,
        "target": next(row["target"] for row in catalog["targets"] if row["id"] == TARGET_ID),
        "protocolVersion": PROTOCOL,
        "contentDigest": archive_digest,
        "artifact": {
            "kind": "data-bundle",
            "format": "tar.zst",
            "uri": release_uri,
            "sha256": archive_digest,
            "sizeBytes": len(archive_raw),
            "files": files,
        },
        "dataBundle": {"proofPath": PROOF_NAME, "proofSha256": _digest(proof_raw)},
        "dependencies": component["dependencies"],
        "restart": component["restart"],
        "source": {"repository": f"https://github.com/{args.repository}", "ref": args.source_ref, "commit": args.source_commit},
        "provenance": {
            "attestation": {
                "kind": "github-artifact-attestation",
                "subjectName": artifact_name,
                "repository": args.repository,
                "workflow": f"{args.repository}/.github/workflows/data-bundle-release.yml",
                "predicateType": "https://slsa.dev/provenance/v1",
                "run": {"id": args.run_id, "attempt": args.run_attempt, "url": run_url},
            }
        },
        "compatibility": {
            "groupId": group["groupId"],
            "groupVersion": group["groupVersion"],
            "contractApiVersion": group["contractApiVersion"],
            "wireApiVersion": group["wireApiVersion"],
            "contractLock": group["contractLock"],
        },
    }
    manifest_doc["manifestDigest"] = _jcs_digest(manifest_doc, "manifestDigest")
    manifest_path = output / f"{COMPONENT_ID}-{TARGET_ID}.manifest.json"
    manifest_raw = (json.dumps(manifest_doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    manifest_path.write_bytes(manifest_raw)
    index_name = "component-release-index-v1.json"
    index_run_url = f"https://github.com/{args.repository}/actions/runs/{args.run_id}/attempts/{args.run_attempt}"
    index_provenance = {
        "attestation": {
            "kind": "github-artifact-attestation",
            "subjectName": index_name,
            "repository": args.repository,
            "workflow": f"{args.repository}/.github/workflows/data-bundle-release.yml",
            "predicateType": "https://slsa.dev/provenance/v1",
            "run": {"id": args.run_id, "attempt": args.run_attempt, "url": index_run_url},
        }
    }
    index_doc: dict[str, Any] = {
        "schemaVersion": 1,
        "repository": args.repository,
        "channel": args.channel,
        "generatedAt": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": {"repository": f"https://github.com/{args.repository}", "ref": args.source_ref, "commit": args.source_commit},
        "provenance": index_provenance,
        "releases": [{
            "componentId": COMPONENT_ID,
            "version": version,
            "target": manifest_doc["target"],
            "manifestUri": f"https://github.com/{args.repository}/releases/download/{release_id}/{manifest_path.name}",
            "manifestDigest": manifest_doc["manifestDigest"],
        }],
        "compatibilityGroups": [{
            "groupId": group["groupId"],
            "groupVersion": group["groupVersion"],
            "contractApiVersion": group["contractApiVersion"],
            "wireApiVersion": group["wireApiVersion"],
            "contractLock": group["contractLock"],
            "members": [{
                "componentId": COMPONENT_ID,
                "target": manifest_doc["target"],
                "version": version,
                "manifestDigest": manifest_doc["manifestDigest"],
            }],
        }],
    }
    index_doc["indexDigest"] = _jcs_digest(index_doc, "indexDigest")
    (output / index_name).write_text(
        json.dumps(index_doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    schema_root = args.catalog.parent
    common_schema = _read_json(schema_root / "component-release-manifest-v1.schema.json")
    manifest_schema = _read_json(schema_root / "component-release-manifest-v2.schema.json")
    index_schema = _read_json(schema_root / "component-release-index-v1.schema.json")
    proof_schema = _read_json(schema_root / "data-bundle-proof-v1.schema.json")
    registry = Registry().with_resources(
        (
            (common_schema["$id"], Resource.from_contents(common_schema)),
            (manifest_schema["$id"], Resource.from_contents(manifest_schema)),
            (index_schema["$id"], Resource.from_contents(index_schema)),
            (proof_schema["$id"], Resource.from_contents(proof_schema)),
        )
    )
    Draft202012Validator(proof_schema, registry=registry).validate(proof)
    Draft202012Validator(manifest_schema, registry=registry).validate(manifest_doc)
    Draft202012Validator(index_schema, registry=registry).validate(index_doc)
    shutil.rmtree(stage)
    print(json.dumps({"archive": str(archive_path), "contentDigest": archive_digest, "manifest": str(manifest_path)}, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True, help="governance/component-catalog-v1.json")
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--channel", choices=("stable", "preview"), required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--owner-publication", action="append", default=[])
    parser.add_argument("--owner-assets", action="append", default=[])
    parser.add_argument("--owner-commit", action="append", default=[])
    parser.add_argument("--policy-publication", type=Path, required=True)
    parser.add_argument("--policy-assets", type=Path, required=True)
    parser.add_argument("--policy-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        package(args)
    except (OSError, ReleaseError, KeyError, TypeError, ValueError, zstandard.ZstdError) as error:
        print(f"data-bundle release packaging failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
