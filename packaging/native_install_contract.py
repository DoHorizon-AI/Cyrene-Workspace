#!/usr/bin/env python3
"""Write the evidence marker for a stage-only native package initialization."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import stat
import sys
from pathlib import Path
from typing import Any

SERVICES = (
    "cyrene-catalyst",
    "cyrene-exchange",
    "cyrene-navigator",
    "cyrene-reactor",
    "cyrene-yield",
)
TARGETS = frozenset(
    {
        "linux-ubuntu-22.04-x86_64-python-3.12",
        "linux-ubuntu-24.04-x86_64-python-3.12",
    }
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RELEASE = re.compile(r"^(stable|preview)-([0-9a-f]{40})$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
WORKSPACE_REPOSITORY = "DoHorizon-AI/Cyrene-Workspace"
UNIT_DIRECTORY = "lib/systemd/system"
PRIVATE_PYTHON = "/opt/cyrene/python/3.12.14/bin/python3.12"
SERVICE_RUNNER = "/usr/lib/cyrene/scripts/cyrene.py"


class ContractError(RuntimeError):
    """Raised when the staged source evidence cannot support the contract."""


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{label} must be a non-empty string")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is missing or unsafe: {path}")
    try:
        content = path.read_bytes()
        value = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read {label} at {path}: {error}") from error
    return _object(value, label), content


def _sha_file(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is missing or unsafe: {path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise ContractError(f"cannot hash {label} at {path}: {error}") from error
    return digest.hexdigest()


def _validate_service_unit(path: Path, service: str) -> str:
    """Bind staged unit bytes to the fixed rootless private-Python runner."""

    if path.is_symlink() or not path.is_file():
        raise ContractError(f"managed service unit is missing or unsafe: {path.name}")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o644:
        raise ContractError(f"managed service unit metadata is unsafe: {path.name}")
    try:
        unit_bytes = path.read_bytes()
        lines = unit_bytes.decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ContractError(f"managed service unit is unreadable: {path.name}") from error

    section = ""
    service_values: dict[str, list[str]] = {"User": [], "Group": [], "ExecStart": []}
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "Service" and "=" in line:
            key, value = line.split("=", 1)
            if key in service_values:
                service_values[key].append(value.strip())
    expected_command = [
        PRIVATE_PYTHON,
        "-sE",
        SERVICE_RUNNER,
        "service-run",
        service,
    ]
    try:
        commands = [shlex.split(value) for value in service_values["ExecStart"]]
    except ValueError as error:
        raise ContractError(f"managed service unit command is malformed: {path.name}") from error
    if (
        service_values["User"] != ["cyrene"]
        or service_values["Group"] != ["cyrene"]
        or commands != [expected_command]
    ):
        raise ContractError(f"managed service unit runner differs from policy: {path.name}")
    return hashlib.sha256(unit_bytes).hexdigest()


def managed_units_from_directory(
    unit_directory: Path,
    *,
    target_profile: str,
    source_ref: str,
    source_commit: str,
) -> dict[str, dict[str, Any]]:
    """Project exact DEB member paths and digests for the five Product units."""

    if unit_directory.is_symlink() or not unit_directory.is_dir():
        raise ContractError(f"managed unit directory is missing or unsafe: {unit_directory}")
    if target_profile not in TARGETS:
        raise ContractError("managed units require the selected native Python target profile")
    if (
        not isinstance(source_ref, str)
        or re.fullmatch(r"refs/(heads|tags)/[A-Za-z0-9._/-]{1,200}", source_ref) is None
    ):
        raise ContractError("Workspace source ref must be an exact Git ref")
    if not isinstance(source_commit, str) or COMMIT.fullmatch(source_commit) is None:
        raise ContractError("Workspace source commit must be a lowercase 40-hex commit")

    source = {
        "repository": WORKSPACE_REPOSITORY,
        "ref": source_ref,
        "commit": source_commit,
    }
    result: dict[str, dict[str, Any]] = {}
    for component_id in SERVICES:
        service = component_id.removeprefix("cyrene-")
        unit = f"{component_id}.service"
        digest = _validate_service_unit(unit_directory / unit, service)
        result[component_id] = {
            "componentId": component_id,
            "service": service,
            "unit": unit,
            "packagePath": f"{UNIT_DIRECTORY}/{unit}",
            "sha256": digest,
            "targetProfile": target_profile,
            "source": source,
        }
    return result


def _verified_source_summary(index: dict[str, Any], target_profile: str) -> dict[str, Any]:
    if (
        set(index) != {"schemaVersion", "targetProfile", "services"}
        or index.get("schemaVersion") != 1
        or index.get("targetProfile") != target_profile
        or target_profile not in TARGETS
    ):
        raise ContractError("service artifact index must be the selected target's exact v1 index")
    service_records = _object(index.get("services"), "index.services")
    if set(service_records) != set(SERVICES):
        raise ContractError("service artifact index must contain exactly the five managed services")

    result: dict[str, Any] = {}
    for component_id in SERVICES:
        record = _object(service_records[component_id], f"index.services.{component_id}")
        if set(record) != {
            "componentId",
            "repository",
            "releaseId",
            "source",
            "artifact",
            "manifest",
            "attestation",
        }:
            raise ContractError(f"{component_id} index entry has an invalid field set")
        if record.get("componentId") != component_id:
            raise ContractError(f"{component_id} index entry has a mismatched componentId")
        repository = _string(record.get("repository"), f"{component_id}.repository")
        release_id = _string(record.get("releaseId"), f"{component_id}.releaseId")
        release_match = RELEASE.fullmatch(release_id)
        source = _object(record.get("source"), f"{component_id}.source")
        if set(source) != {"ref", "commit"}:
            raise ContractError(f"{component_id}.source has an invalid field set")
        source_ref = _string(source.get("ref"), f"{component_id}.source.ref")
        source_commit = _string(source.get("commit"), f"{component_id}.source.commit")
        if release_match is None or release_match.group(2) != source_commit:
            raise ContractError(f"{component_id} releaseId must bind its source commit")

        artifact = _object(record.get("artifact"), f"{component_id}.artifact")
        if set(artifact) != {"path", "sha256", "sizeBytes", "kind", "format"}:
            raise ContractError(f"{component_id}.artifact has an invalid field set")
        artifact_sha = _sha(artifact.get("sha256"), f"{component_id}.artifact.sha256")
        if (
            not isinstance(artifact.get("sizeBytes"), int)
            or isinstance(artifact.get("sizeBytes"), bool)
            or artifact["sizeBytes"] < 1
            or artifact.get("kind") != "python-bundle"
            or artifact.get("format") != "tar.gz"
        ):
            raise ContractError(f"{component_id}.artifact metadata is invalid")

        manifest = _object(record.get("manifest"), f"{component_id}.manifest")
        if set(manifest) != {"path", "sha256", "manifestDigest"}:
            raise ContractError(f"{component_id}.manifest has an invalid field set")
        manifest_sha = _sha(manifest.get("sha256"), f"{component_id}.manifest.sha256")
        manifest_digest = _string(
            manifest.get("manifestDigest"), f"{component_id}.manifest.manifestDigest"
        )
        if re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_digest) is None:
            raise ContractError(f"{component_id}.manifest.manifestDigest is invalid")

        attestation = _object(record.get("attestation"), f"{component_id}.attestation")
        if set(attestation) != {
            "path",
            "sha256",
            "repository",
            "workflow",
            "predicateType",
            "subjectName",
            "sourceRef",
            "sourceCommit",
        }:
            raise ContractError(f"{component_id}.attestation has an invalid field set")
        attestation_sha = _sha(attestation.get("sha256"), f"{component_id}.attestation.sha256")
        if (
            attestation.get("repository") != repository
            or attestation.get("predicateType") != "https://slsa.dev/provenance/v1"
            or attestation.get("sourceRef") != source_ref
            or attestation.get("sourceCommit") != source_commit
        ):
            raise ContractError(
                f"{component_id}.attestation identity differs from its source tuple"
            )

        result[component_id] = {
            "componentId": component_id,
            "repository": repository,
            "releaseId": release_id,
            "source": {"ref": source_ref, "commit": source_commit},
            "artifact": {"sha256": artifact_sha, "sizeBytes": artifact["sizeBytes"]},
            "manifest": {"sha256": manifest_sha, "manifestDigest": manifest_digest},
            "attestation": {
                "sha256": attestation_sha,
                "repository": attestation["repository"],
                "workflow": _string(
                    attestation.get("workflow"), f"{component_id}.attestation.workflow"
                ),
                "subjectName": _string(
                    attestation.get("subjectName"), f"{component_id}.attestation.subjectName"
                ),
                "sourceRef": source_ref,
                "sourceCommit": source_commit,
            },
        }
    return result


def create_contract(
    *,
    target_profile: str,
    service_artifacts_index: Path,
    scripts_dir: Path,
    unit_directory: Path,
    source_ref: str,
    source_commit: str,
    output: Path,
) -> dict[str, Any]:
    """Create a marker bound to the installed source index and actual scripts."""

    if scripts_dir.is_symlink() or not scripts_dir.is_dir():
        raise ContractError(f"maintainer script directory is missing or unsafe: {scripts_dir}")
    index, index_bytes = _read_json(service_artifacts_index, "service artifacts index")
    scripts = {
        name: _sha_file(scripts_dir / name, f"DEBIAN/{name}")
        for name in ("postinst", "prerm", "postrm")
    }
    contract = {
        "schemaVersion": 1,
        "targetProfile": target_profile,
        "initializationMode": "stage-only",
        "serviceArtifactsMode": "verified-published-bytes",
        "serviceActivation": "deferred",
        "brokerAction": "preserve-existing",
        "oldRuntimeAction": "preserve",
        "serviceArtifactsIndexSha256": hashlib.sha256(index_bytes).hexdigest(),
        "services": _verified_source_summary(index, target_profile),
        "managedUnits": managed_units_from_directory(
            unit_directory,
            target_profile=target_profile,
            source_ref=source_ref,
            source_commit=source_commit,
        ),
        "maintainerScriptsSha256": scripts,
    }
    if output.exists() or output.is_symlink():
        raise ContractError(f"contract output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o644)
    return contract


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create the Cyrene stage-only package marker")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "create", help="bind source artifact evidence and maintainer scripts"
    )
    create.add_argument("--target-profile", required=True)
    create.add_argument("--service-artifacts-index", required=True, type=Path)
    create.add_argument("--scripts-dir", required=True, type=Path)
    create.add_argument("--unit-directory", required=True, type=Path)
    create.add_argument("--source-ref", required=True)
    create.add_argument("--source-commit", required=True)
    create.add_argument("--output", required=True, type=Path)
    create.set_defaults(handler=_create_command)
    return parser


def _create_command(args: argparse.Namespace) -> int:
    create_contract(
        target_profile=args.target_profile,
        service_artifacts_index=args.service_artifacts_index,
        scripts_dir=args.scripts_dir,
        unit_directory=args.unit_directory,
        source_ref=args.source_ref,
        source_commit=args.source_commit,
        output=args.output,
    )
    print(
        json.dumps({"contractSha256": _sha_file(args.output, "generated contract")}, sort_keys=True)
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return arguments.handler(arguments)
    except ContractError as error:
        print(f"native-install-contract: ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
