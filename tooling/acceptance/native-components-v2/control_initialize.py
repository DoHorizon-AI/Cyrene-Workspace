#!/usr/bin/env python3
"""Read-only preflight for the production Workspace control plane.

This module validates explicit signed data-bundle inputs and the metadata of
administrator-provisioned Authority files. It never installs packages,
creates trust, runs migrations, contacts services, or changes updater state.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
UPDATER_PATH = WORKSPACE_ROOT / "packaging/component_updates.py"
DEFAULT_CATALOG = WORKSPACE_ROOT / "packaging/component-catalog-bootstrap-v1.json"
COMPONENT_ID = "cyrene-product-contract-bundle"
DATA_BUNDLE_TARGET = "portable-contract-data-v1"
TRUSTED_CATALOG_DIGEST = "sha256:fc2d6dc485bfc6a6fbbef426c93acf2a640b771e6e24a7284b030804fd02fab0"
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TENANT_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
NONSECRET_AUTHORITY_KEYS = frozenset(
    {
        "CYRENE_AUTHORITY_AAD_TENANT_ID",
        "CYRENE_AUTHORITY_AAD_AUDIENCE",
        "CYRENE_AUTHORITY_SIGNING_KEY_ID",
        "CYRENE_AUTHORITY_DATABASE_URL_FILE",
        "CYRENE_AUTHORITY_SIGNING_KEY_FILE",
        "CYRENE_AUTHORITY_TLS_CERT_FILE",
        "CYRENE_AUTHORITY_TLS_KEY_FILE",
        "CYRENE_AUTHORITY_TLS_CLIENT_CA_FILE",
        "CYRENE_WORKSPACE_AUTHORITY_TRUST_CONFIG",
        "CYRENE_AUTHORITY_INITIAL_ARTIFACT_ID_FILE",
    }
)
MIGRATION_DATABASE_KEYS = frozenset(
    {
        "CYRENE_WORKSPACE_DIRECTORY_MIGRATION_DATABASE_URL",
        "CYRENE_WORKSPACE_DEVICE_AUTHORIZATION_MIGRATION_DATABASE_URL",
        "CYRENE_WORKSPACE_DEVICE_REGISTRY_MIGRATION_DATABASE_URL",
        "CYRENE_WORKSPACE_WEBAUTHN_MIGRATION_DATABASE_URL",
        "CYRENE_WORKSPACE_WEBAUTHN_HTTP_SESSION_BINDING_MIGRATION_DATABASE_URL",
        "CYRENE_WORKSPACE_DEVICE_CA_MIGRATION_DATABASE_URL",
    }
)
CA_CONFIG_KEYS = frozenset(
    {
        "CYRENE_WORKSPACE_DEVICE_CA_DATABASE_URL",
        "CYRENE_WORKSPACE_DEVICE_CA_SIGNING_KEY_FILE",
        "CYRENE_WORKSPACE_DEVICE_CA_CERTIFICATE_FILE",
        "CYRENE_WORKSPACE_DEVICE_CA_ISSUER_ID",
    }
)


class ControlInitializationError(ValueError):
    """An unsafe, incomplete, or mismatched initialization precondition."""


def _load_updater() -> Any:
    spec = importlib.util.spec_from_file_location(
        "cyrene_component_updates_preflight", UPDATER_PATH
    )
    if spec is None or spec.loader is None:
        raise ControlInitializationError("official component updater source is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _regular_input(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise ControlInitializationError(f"{label} is missing or unreadable") from error
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
        raise ControlInitializationError(f"{label} must be a non-writable regular file")
    return info


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _json_file(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    _regular_input(path, label)
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlInitializationError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ControlInitializationError(f"{label} must be a JSON object")
    return value, payload


def _read_allowlisted_env(
    path: Path, allowed: frozenset[str], label: str, *, uid: int | None = None
) -> dict[str, str]:
    """Read only allowlisted nonsecret assignments and never retain other values."""
    info = _regular_input(path, label)
    if uid is not None and info.st_uid != uid:
        raise ControlInitializationError(f"{label} must be owned by the control administrator")
    result: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8") as stream:
            for raw_line in stream:
                key, separator, raw_value = raw_line.strip().partition("=")
                if not separator or key not in allowed:
                    continue
                value = raw_value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                result[key] = value
    except (OSError, UnicodeDecodeError) as error:
        raise ControlInitializationError(f"{label} cannot be inspected") from error
    return result


def _require_env_keys_without_values(
    path: Path, required: frozenset[str], label: str, *, uid: int | None = None
) -> None:
    """Check secret-bearing EnvironmentFile key names without retaining values."""
    info = _regular_input(path, label)
    if uid is not None and info.st_uid != uid:
        raise ControlInitializationError(f"{label} must be owned by the control administrator")
    found: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as stream:
            for raw_line in stream:
                assignment = raw_line.strip()
                if assignment.startswith("export "):
                    assignment = assignment[7:].lstrip()
                key, separator, discarded_value = assignment.partition("=")
                if separator and discarded_value.strip() and key in required:
                    found.add(key)
    except (OSError, UnicodeDecodeError) as error:
        raise ControlInitializationError(f"{label} cannot be inspected") from error
    if found != required:
        raise ControlInitializationError(f"{label} is missing required production setting names")


def _check_owned_path(
    path: Path,
    *,
    label: str,
    uid: int,
    allowed_modes: int,
    private_parent: bool = False,
) -> dict[str, Any]:
    """Inspect a secret/config target without opening it or exposing its contents."""
    try:
        info = path.lstat()
    except OSError as error:
        raise ControlInitializationError(f"required {label} path is missing") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) & ~allowed_modes
    ):
        raise ControlInitializationError(f"required {label} path has unsafe type, owner, or mode")
    if private_parent:
        try:
            parent_info = path.parent.lstat()
        except OSError as error:
            raise ControlInitializationError(
                f"required {label} parent directory is unavailable"
            ) from error
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != uid
            or stat.S_IMODE(parent_info.st_mode) & 0o077
        ):
            raise ControlInitializationError(f"required {label} parent directory is unsafe")
    return {"path": str(path), "ownerUid": info.st_uid, "mode": oct(stat.S_IMODE(info.st_mode))}


def _protected_directory(
    path: Path,
    *,
    uid: int = 0,
    allow_missing: bool = True,
    boundary: Path = Path("/"),
) -> dict[str, Any]:
    """Check existing ancestors and directories without creating anything."""
    try:
        relative = path.relative_to(boundary)
    except ValueError as error:
        raise ControlInitializationError(
            "protected directory escapes the inspected filesystem root"
        ) from error
    cursor = boundary
    missing: list[str] = []
    if boundary != Path("/"):
        try:
            boundary_info = boundary.lstat()
        except OSError as error:
            raise ControlInitializationError("filesystem root cannot be inspected") from error
        if (
            not stat.S_ISDIR(boundary_info.st_mode)
            or boundary_info.st_uid != uid
            or stat.S_IMODE(boundary_info.st_mode) & 0o022
        ):
            raise ControlInitializationError(
                "filesystem root for protected-path inspection is unsafe"
            )
    for part in relative.parts:
        cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            missing.append(str(cursor))
            continue
        except OSError as error:
            raise ControlInitializationError(
                f"cannot inspect protected directory {cursor}"
            ) from error
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise ControlInitializationError(f"protected directory is unsafe: {cursor}")
        if missing:
            raise ControlInitializationError(
                f"protected directory has an unknown gap before {cursor}"
            )
    if missing and not allow_missing:
        raise ControlInitializationError(f"protected directory is missing: {missing[-1]}")
    return {
        "path": str(path),
        "state": "missing" if missing else "present-safe",
        "missing": missing,
    }


def _verify_existing_selector(
    path: Path, expected_artifact_id: str, *, uid: int = 0
) -> dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "state": "absent"}
    except OSError as error:
        raise ControlInitializationError("initial artifact selector cannot be inspected") from error
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o027:
        raise ControlInitializationError("existing initial artifact selector is unsafe")
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise ControlInitializationError(
            "existing initial artifact selector is unreadable"
        ) from error
    if value != expected_artifact_id:
        raise ControlInitializationError(
            "existing initial artifact selector does not match the verified bundle"
        )
    return {
        "path": str(path),
        "state": "matches-verified-artifact",
        "ownerUid": info.st_uid,
        "mode": oct(stat.S_IMODE(info.st_mode)),
    }


def _verify_existing_trust(path: Path, expected_digest: str, *, uid: int = 0) -> dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "state": "absent"}
    except OSError as error:
        raise ControlInitializationError(
            "existing Authority trust config cannot be inspected"
        ) from error
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o027:
        raise ControlInitializationError("existing Authority trust config is unsafe")
    if _file_digest(path) != expected_digest:
        raise ControlInitializationError(
            "existing Authority trust config differs from the reviewed digest"
        )
    return {
        "path": str(path),
        "state": "matches-reviewed-digest",
        "ownerUid": info.st_uid,
        "mode": oct(stat.S_IMODE(info.st_mode)),
    }


def _select_bundle_release_entry(
    index: dict[str, Any], component: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    """Select the unique release row using the emitter's complete target object."""
    releases = index.get("releases")
    if not isinstance(releases, list):
        raise ControlInitializationError("signed index release list is invalid")
    matches = [
        item
        for item in releases
        if isinstance(item, dict)
        and item.get("componentId") == component.get("componentId")
        and item.get("target") == target.get("target")
    ]
    if len(matches) != 1:
        raise ControlInitializationError(
            "signed index must contain exactly one data-bundle release for the trusted full target"
        )
    return matches[0]


def _validate_indexed_manifest(
    updater: Any,
    manifest: dict[str, Any],
    index: dict[str, Any],
    component: dict[str, Any],
    target: dict[str, Any],
    publisher: dict[str, Any],
    channel: str,
) -> dict[str, Any]:
    entry = _select_bundle_release_entry(index, component, target)
    updater._require_github_asset_uri(entry.get("manifestUri"), publisher["repository"])
    updater._validate_manifest(manifest, entry, component, target, publisher, channel, index)
    return entry


def _validate_manifest_index_digest(
    updater_module: Any, manifest: dict[str, Any], entry: dict[str, Any]
) -> None:
    """Check the canonical manifest digest pinned by the signed release index."""
    digest = updater_module._digest_json(manifest, "manifestDigest")
    if manifest.get("manifestDigest") != digest or entry.get("manifestDigest") != digest:
        raise ControlInitializationError(
            "outer bundle manifest canonical digest does not match the signed index"
        )


def _bundle_target(updater: Any, component: dict[str, Any]) -> dict[str, Any]:
    """Combine the catalog target with its unique component artifact declaration."""
    target = updater.targets.get(DATA_BUNDLE_TARGET)
    declarations = [
        item
        for item in component.get("targets", [])
        if isinstance(item, dict) and item.get("targetId") == DATA_BUNDLE_TARGET
    ]
    if (
        not isinstance(target, dict)
        or len(declarations) != 1
        or declarations[0].get("artifactKind") != "data-bundle"
        or declarations[0].get("support") != "supported"
    ):
        raise ControlInitializationError(
            "trusted catalog does not uniquely authorize the data-bundle target"
        )
    return {**target, "artifactKind": declarations[0]["artifactKind"]}


def _bundle_validation(
    args: argparse.Namespace, *, updater_module: Any, runner: Callable[..., Any]
) -> dict[str, Any]:
    catalog_path = Path(args.catalog)
    _regular_input(catalog_path, "trusted component catalog")
    catalog_digest = _file_digest(catalog_path)
    if catalog_digest != updater_module.TRUSTED_CATALOG_DIGEST:
        raise ControlInitializationError(
            "trusted component catalog does not match the compiled authority digest"
        )

    updater = updater_module.ComponentUpdater(
        catalog_path=catalog_path,
        trusted_catalog_digest=updater_module.TRUSTED_CATALOG_DIGEST,
        opener=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("network access is forbidden")
        ),
        runner=runner,
        load_active_catalog=False,
    )
    component = updater.components.get(COMPONENT_ID)
    publisher = updater.publishers.get(component.get("publisher")) if component else None
    if component is None or publisher is None:
        raise ControlInitializationError(
            "trusted catalog omits the required data-bundle component or target"
        )
    target = _bundle_target(updater, component)

    index, index_bytes = _json_file(Path(args.index), "release index")
    index_digest = "sha256:" + hashlib.sha256(index_bytes).hexdigest()
    tag_prefix = updater._component_release_tag_prefix(component, args.channel) or (
        "preview-" if args.channel == "preview" else "stable-"
    )
    release = {"tag_name": tag_prefix + str(index.get("source", {}).get("commit", ""))}
    updater._validate_index(index, publisher, args.channel, release, component)
    entry = _select_bundle_release_entry(index, component, target)
    manifest, manifest_bytes = _json_file(Path(args.manifest), "outer bundle manifest")
    # The release index pins a JCS digest; raw asset bytes have a separate
    # release-API digest and are verified at download time.
    _validate_manifest_index_digest(updater_module, manifest, entry)
    _validate_indexed_manifest(updater, manifest, index, component, target, publisher, args.channel)
    artifact = manifest.get("artifact", {})
    artifact_path = Path(args.artifact)
    artifact_info = _regular_input(artifact_path, "signed data-bundle artifact")
    artifact_digest = _file_digest(artifact_path)
    if artifact_digest != artifact.get("sha256") or artifact_info.st_size != artifact.get(
        "sizeBytes"
    ):
        raise ControlInitializationError(
            "data-bundle artifact digest or size does not match its verified manifest"
        )

    # The official verifier is run only against explicitly supplied local bundles.
    # No release lookup, network request, updater check/stage/apply, or persistent
    # update state is touched by this preflight.
    index_attestation = Path(args.index_attestation)
    artifact_attestation = Path(args.artifact_attestation)
    _regular_input(index_attestation, "index attestation bundle")
    _regular_input(artifact_attestation, "artifact attestation bundle")
    updater._verify_attestation(
        index_bytes,
        subject_name=publisher["releaseDiscovery"]["indexAssetName"],
        digest=index_digest,
        repository=publisher["repository"],
        workflow=publisher["workflow"],
        source_ref=index["source"]["ref"],
        source_commit=index["source"]["commit"],
        bundle_path=index_attestation,
    )
    updater._verify_attestation(
        artifact_path.read_bytes(),
        subject_name=manifest["provenance"]["attestation"]["subjectName"],
        digest=artifact_digest,
        repository=publisher["repository"],
        workflow=publisher["workflow"],
        source_ref=manifest["source"]["ref"],
        source_commit=manifest["source"]["commit"],
        bundle_path=artifact_attestation,
    )

    # Bundle expansion and proof validation use the official updater's bounded,
    # link-rejecting routines in a temporary directory; they do not stage/import.
    with tempfile.TemporaryDirectory(prefix="cyrene-control-preflight-") as scratch:
        scratch_root = Path(scratch)
        archive_copy = scratch_root / "artifact.tar.zst"
        archive_copy.write_bytes(artifact_path.read_bytes())
        extracted = scratch_root / "payload"
        updater._extract_data_bundle_archive(archive_copy, extracted, artifact)
        candidate = updater_module.Candidate(
            component=component,
            manifest=manifest,
            manifest_digest=entry["manifestDigest"],
            artifact_digest=artifact_digest,
            manifest_uri=entry["manifestUri"],
            index=index,
            index_uri="explicit-local-input",
            manifest_bytes=manifest_bytes,
        )
        proof = updater._validate_data_bundle_proof(candidate, extracted, args.channel)

    return {
        "status": "PASS",
        "componentId": COMPONENT_ID,
        "artifactId": artifact_digest,
        "version": manifest["version"],
        "channel": args.channel,
        "source": {
            "repository": index["source"]["repository"],
            "ref": index["source"]["ref"],
            "commit": index["source"]["commit"],
        },
        "catalogDigest": catalog_digest,
        "indexDigest": index_digest,
        "manifestDigest": entry["manifestDigest"],
        "artifactDigest": artifact_digest,
        "proofSchemaVersion": proof["schemaVersion"],
        "signatureVerification": "PASS (official gh attestation verifier; explicit local bundles)",
    }


def build_plan(
    args: argparse.Namespace,
    *,
    updater_module: Any | None = None,
    runner: Callable[..., Any] = subprocess.run,
    filesystem_root: Path = Path("/"),
    uid: int | None = None,
) -> dict[str, Any]:
    """Return a read-only report; no initialization or updater mutation occurs."""
    if uid is None and os.geteuid() != 0:
        raise ControlInitializationError("production control preflight must run as root")
    uid = os.geteuid() if uid is None else uid
    service_uid = getattr(args, "service_uid", None)
    if service_uid is None:
        try:
            service_uid = pwd.getpwnam("cyrene").pw_uid
        except KeyError as error:
            raise ControlInitializationError(
                "installed Authority service account is unavailable"
            ) from error
    updater_module = updater_module or _load_updater()
    if args.channel not in {"stable", "preview"}:
        raise ControlInitializationError("channel must be stable or preview")
    if args.control_ubuntu not in {"22.04", "24.04"}:
        raise ControlInitializationError("control host must be Ubuntu 22.04 or 24.04")
    if not TENANT_RE.fullmatch(args.tenant_id):
        raise ControlInitializationError("a real Entra tenant UUID is required")
    if not args.audience or args.audience != args.audience.strip() or "\n" in args.audience:
        raise ControlInitializationError("the configured Authority audience is required")
    if not SHA256_RE.fullmatch(args.trust_config_sha256):
        raise ControlInitializationError("a separately reviewed trust-config SHA-256 is required")

    bundle = _bundle_validation(args, updater_module=updater_module, runner=runner)
    root = Path(filesystem_root)

    def rooted(path: str) -> Path:
        absolute = Path(path)
        if not absolute.is_absolute():
            raise ControlInitializationError("target configuration paths must be absolute")
        return root / absolute.relative_to("/") if root != Path("/") else absolute

    authority_root = rooted("/etc/cyrene-workspace-authority")
    bundle_root = rooted("/var/lib/cyrene-product-bundles")
    authority_dir_check = _protected_directory(authority_root, uid=uid, boundary=root)
    _protected_directory(bundle_root, uid=uid, boundary=root)
    trust_path = rooted(args.trust_config)
    selector_path = rooted(args.initial_artifact_id)
    trust_state = _verify_existing_trust(trust_path, args.trust_config_sha256, uid=uid)
    selector_state = _verify_existing_selector(selector_path, bundle["artifactId"], uid=uid)

    authority_values = _read_allowlisted_env(
        Path(args.authority_env), NONSECRET_AUTHORITY_KEYS, "Authority environment file", uid=uid
    )
    identity_values = _read_allowlisted_env(
        Path(args.identity_env),
        NONSECRET_AUTHORITY_KEYS,
        "Authority identity environment file",
        uid=uid,
    )
    env_values = authority_values | identity_values
    if env_values.get("CYRENE_AUTHORITY_AAD_TENANT_ID") != args.tenant_id:
        raise ControlInitializationError(
            "Authority environment tenant does not match the explicit tenant input"
        )
    if env_values.get("CYRENE_AUTHORITY_AAD_AUDIENCE") != args.audience:
        raise ControlInitializationError(
            "Authority environment audience does not match the explicit audience input"
        )
    if env_values.get("CYRENE_WORKSPACE_AUTHORITY_TRUST_CONFIG") != args.trust_config:
        raise ControlInitializationError(
            "Authority unit does not reference the reviewed trust-config path"
        )
    if (
        env_values.get(
            "CYRENE_AUTHORITY_INITIAL_ARTIFACT_ID_FILE",
            "/etc/cyrene-workspace-authority/initial-artifact-id",
        )
        != args.initial_artifact_id
    ):
        raise ControlInitializationError(
            "Authority unit does not reference the reviewed initial artifact selector"
        )

    required_authority = {
        "CYRENE_AUTHORITY_DATABASE_URL_FILE": 0o600,
        "CYRENE_AUTHORITY_SIGNING_KEY_FILE": 0o600,
        "CYRENE_AUTHORITY_TLS_CERT_FILE": 0o600,
        "CYRENE_AUTHORITY_TLS_KEY_FILE": 0o600,
        "CYRENE_AUTHORITY_TLS_CLIENT_CA_FILE": 0o600,
    }
    secrets: dict[str, dict[str, Any]] = {}
    for key, mode_mask in required_authority.items():
        value = env_values.get(key)
        if not value:
            raise ControlInitializationError(
                f"Authority configuration is missing required path setting {key}"
            )
        secrets[key] = _check_owned_path(
            Path(value),
            label=key,
            uid=service_uid,
            allowed_modes=mode_mask,
            private_parent=(key == "CYRENE_AUTHORITY_SIGNING_KEY_FILE"),
        )
    if not env_values.get("CYRENE_AUTHORITY_SIGNING_KEY_ID"):
        raise ControlInitializationError("Authority signing key identifier is missing")

    _require_env_keys_without_values(
        Path(args.migration_env), MIGRATION_DATABASE_KEYS, "migration environment file", uid=uid
    )
    migration_paths = {
        "requiredSettingNames": sorted(MIGRATION_DATABASE_KEYS),
        "valuesReadOrEmitted": False,
    }

    _require_env_keys_without_values(
        Path(args.device_ca_env), CA_CONFIG_KEYS, "device CA environment file", uid=uid
    )
    ca_env = _read_allowlisted_env(
        Path(args.device_ca_env),
        CA_CONFIG_KEYS - {"CYRENE_WORKSPACE_DEVICE_CA_DATABASE_URL"},
        "device CA environment file",
        uid=uid,
    )
    if not ca_env.get("CYRENE_WORKSPACE_DEVICE_CA_ISSUER_ID"):
        raise ControlInitializationError(
            "real device CA runtime URL, issuer key/certificate, and issuer ID are required"
        )
    ca_files = {
        key: _check_owned_path(
            Path(ca_env[key]),
            label=key,
            uid=service_uid,
            allowed_modes=0o600,
            private_parent=("SIGNING_KEY_FILE" in key),
        )
        for key in (
            "CYRENE_WORKSPACE_DEVICE_CA_SIGNING_KEY_FILE",
            "CYRENE_WORKSPACE_DEVICE_CA_CERTIFICATE_FILE",
        )
        if ca_env.get(key)
    }
    if len(ca_files) != 2:
        raise ControlInitializationError(
            "device CA database, signer key, and issuer certificate paths are required"
        )

    existing_bundle = _protected_directory(bundle_root, allow_missing=True, uid=uid, boundary=root)
    return {
        "schemaVersion": 1,
        "mode": "read-only-plan",
        "status": "PLAN_ONLY",
        "mutationsPerformed": [],
        "signatureAndBundle": bundle,
        "target": {
            "controlUbuntu": args.control_ubuntu,
            "authorityRoot": authority_dir_check,
            "dataBundleRoot": existing_bundle,
        },
        "authority": {
            "trustConfig": trust_state,
            "initialArtifactSelector": selector_state,
            "tenantId": args.tenant_id,
            "audience": args.audience,
            "secretFiles": secrets,
            "migrationFiles": migration_paths,
            "deviceCaFiles": ca_files,
            "privateContentsReadOrEmitted": False,
        },
        "execution": {
            "thisModuleProvisionedAnything": False,
            "migrationsApplied": False,
            "serviceStarted": False,
            "officialNextSteps": [
                "install the reviewed Workspace native DEB through admin_initialize.py (operator-approved one-time packet)",
                "run cy-workspace-storage-migrator migrate-all with the six component-specific *_MIGRATION_DATABASE_URL environment variables (includes the device CA schema)",
                "run cy-workspace-device-ca-admin check with its runtime URL and protected issuer key/certificate paths",
                "run the official component-updates check for cyrene-product-contract-bundle, then stage after explicit digest confirmation",
                "review/provision root-owned Authority trust.json and initial-artifact-id from the verified artifact ID",
                "start cyrene-workspace-authority.service only after PostgreSQL, Directory, AAD/JWKS, TLS, and imported bundle readiness are independently confirmed",
                "use cy-workspace-authority-admin status, validate-artifact, activate-artifact in that order for subsequent bundle activations",
            ],
            "recoveryRecord": "NOT_CREATED; no transaction was begun",
            "acceptance": "does not provision Authority/Relay/BFF or establish caller-token identity; old manual runtime remains UNKNOWN",
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--index-attestation", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--artifact-attestation", type=Path, required=True)
    parser.add_argument("--control-ubuntu", required=True)
    parser.add_argument("--authority-env", type=Path, required=True)
    parser.add_argument("--identity-env", type=Path, required=True)
    parser.add_argument("--migration-env", type=Path, required=True)
    parser.add_argument("--device-ca-env", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--trust-config", default="/etc/cyrene-workspace-authority/trust.json")
    parser.add_argument("--trust-config-sha256", required=True)
    parser.add_argument(
        "--initial-artifact-id", default="/etc/cyrene-workspace-authority/initial-artifact-id"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        report = build_plan(_parser().parse_args(argv))
    except (
        ControlInitializationError,
        RuntimeError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        subprocess.SubprocessError,
    ):
        # Errors are deliberately detail-free so parser and OS errors cannot
        # accidentally include a secret environment assignment or CLI output.
        print(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "mode": "read-only-plan",
                    "status": "BLOCKED",
                    "reason": "preflight could not prove every required condition; no state changed",
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
