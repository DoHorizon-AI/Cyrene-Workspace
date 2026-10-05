"""Focused trust-boundary tests for first Package Runtime admission."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "packaging/native_package_runtime_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("native_package_runtime_bootstrap", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_release(tmp_path: Path) -> tuple[Path, Path, Path]:
    assets = tmp_path / "assets"
    attestations = tmp_path / "attestations"
    assets.mkdir(mode=0o700)
    attestations.mkdir(mode=0o700)
    plugin_manifest = _json_bytes(
        {
            "schemaVersion": 1,
            "id": bootstrap.PACKAGE_ID,
            "version": bootstrap.PACKAGE_VERSION,
            "capabilities": [bootstrap.PACKAGE_CAPABILITY],
            "runtime": {"entrypoint": "llama_factory:LlamaFactoryTrainingPlugin"},
        }
    )
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("plugin.manifest.json", plugin_manifest)
        archive.writestr("requirements.lock", b"package==1.2.3 --hash=sha256:" + b"a" * 64)
    bodies = {
        "archive": archive_buffer.getvalue(),
        "descriptor": b"descriptor fixture",
        "dependency_lock": b"package==1.2.3 --hash=sha256:" + b"a" * 64,
        "preparer_wheel": b"preparer wheel fixture",
    }
    payload_hashes = {key: _digest(body) for key, body in bodies.items()}
    payload_paths = {key: assets / bootstrap.ASSET_NAMES[key] for key in bodies}
    for key, path in payload_paths.items():
        path.write_bytes(bodies[key])

    artifact_uri = (
        f"https://github.com/{bootstrap.PACKAGE_SOURCE_REPOSITORY}/releases/download/"
        f"preview-{bootstrap.PACKAGE_ID}-{bootstrap.PACKAGE_VERSION}-"
        f"{bootstrap.PACKAGE_SOURCE_COMMIT}/{bootstrap.ASSET_NAMES['archive']}"
    )
    metadata = {
        "artifact_uri": artifact_uri,
        "assets": {
            "package": {
                "content_sha256": payload_hashes["archive"],
                "format": "zip",
                "name": bootstrap.ASSET_NAMES["archive"],
                "sha256": payload_hashes["archive"],
            },
            "descriptor": {
                "format": "json",
                "name": bootstrap.ASSET_NAMES["descriptor"],
                "sha256": payload_hashes["descriptor"],
            },
            "dependency_lock": {
                "format": "requirements.lock",
                "name": bootstrap.ASSET_NAMES["dependency_lock"],
                "package_ref": "requirements.lock",
                "sha256": payload_hashes["dependency_lock"],
            },
            "preparer_wheel": {
                "entrypoint": "cyrene-plugin-python-preparer",
                "format": "wheel",
                "name": bootstrap.ASSET_NAMES["preparer_wheel"],
                "package": "cyrene-plugin-runtime",
                "sha256": payload_hashes["preparer_wheel"],
                "target": "py3-none-any",
                "version": "0.2.0",
            },
        },
        "attestation_policy": {
            "provider": "github-artifact-attestation",
            "source_commit": bootstrap.PACKAGE_SOURCE_COMMIT,
            "subject_assets": [
                {"name": bootstrap.ASSET_NAMES[key], "sha256": payload_hashes[key]}
                for key in bootstrap._SUBJECT_KEYS
            ],
            "workflow": bootstrap.PACKAGE_WORKFLOW,
        },
        "channel": bootstrap.PACKAGE_CHANNEL,
        "package": {
            "capability": bootstrap.PACKAGE_CAPABILITY,
            "entrypoint": "llama_factory:LlamaFactoryTrainingPlugin",
            "id": bootstrap.PACKAGE_ID,
            "interface_version": bootstrap.PACKAGE_INTERFACE_VERSION,
            "version": bootstrap.PACKAGE_VERSION,
        },
        "publication_status": "PUBLISHED",
        "record_type": "cyrene.plugin.package.release.v1",
        "release_tag": (
            f"preview-{bootstrap.PACKAGE_ID}-{bootstrap.PACKAGE_VERSION}-"
            f"{bootstrap.PACKAGE_SOURCE_COMMIT}"
        ),
        "runtime": {
            "launch_executable": "prepared-runtime",
            "preparer_command": "cyrene-plugin-python-preparer",
            "preparer_configuration": {
                "arguments": ["--uv", "<uv executable>", "--python", "<python >=3.11 executable>"],
                "evidence_protocol": "cyrene.package-dependency-preparer.v1",
                "host_api": "cy-package-runtime::CommandDependencyPreparer",
                "runtime_executable_is_consumed_by": "ProcessPluginServiceSupervisor",
            },
            "preparer_package": "cyrene-plugin-runtime==0.2.0",
            "preparer_wheel_install_command": [
                "<python >=3.11 executable>",
                "-m",
                "pip",
                "install",
                "--no-deps",
                "<verified preparer wheel path>",
            ],
            "protocol": "cyrene.plugin.runtime.v1.DirectPluginRuntime",
        },
        "source": {
            "commit": bootstrap.PACKAGE_SOURCE_COMMIT,
            "input_tree_digest": _digest(b"input tree"),
            "inputs": [{"path": "README.md", "sha256": _digest(b"source file")}],
            "ref": bootstrap.PACKAGE_SOURCE_REF,
            "repository": bootstrap.PACKAGE_SOURCE_URL,
        },
        "spec_version": "1",
        "target": {
            "architecture": "x86_64",
            "id": bootstrap.PACKAGE_TARGET_ID,
            "os": "linux",
            "python": ">=3.11",
        },
    }
    descriptor = {
        "capability": {
            "id": bootstrap.PACKAGE_CAPABILITY,
            "interface_version": bootstrap.PACKAGE_INTERFACE_VERSION,
        },
        "dependencies": {
            "lock": {
                "digest": payload_hashes["dependency_lock"],
                "format": "requirements.lock",
                "ref": "requirements.lock",
                "status": "LOCKED",
            }
        },
        "implementation": {
            "artifact": {
                "digest": payload_hashes["archive"],
                "format": "zip",
                "status": "PUBLISHED",
                "uri": artifact_uri,
            },
            "entrypoint": "llama_factory:LlamaFactoryTrainingPlugin",
        },
        "integrity": {
            "archive_digest": payload_hashes["archive"],
            "artifact_digest": payload_hashes["archive"],
            "signature_ref": None,
        },
        "package": {
            "id": bootstrap.PACKAGE_ID,
            "manifest_ref": "plugin.manifest.json",
            "version": bootstrap.PACKAGE_VERSION,
        },
        "provenance": {
            "attestation_ref": None,
            "builder": bootstrap.PACKAGE_WORKFLOW,
            "source_repository": bootstrap.PACKAGE_SOURCE_URL,
            "source_revision": bootstrap.PACKAGE_SOURCE_COMMIT,
        },
        "publication_status": "PUBLISHED",
        "record_type": "package_descriptor",
        "spec_version": "0.1",
    }
    descriptor_bytes = _json_bytes(descriptor)
    payload_hashes["descriptor"] = _digest(descriptor_bytes)
    metadata["assets"]["descriptor"]["sha256"] = payload_hashes["descriptor"]
    for subject in metadata["attestation_policy"]["subject_assets"]:
        if subject["name"] == bootstrap.ASSET_NAMES["descriptor"]:
            subject["sha256"] = payload_hashes["descriptor"]
    release_path = assets / bootstrap.ASSET_NAMES["package_release"]
    release_path.write_bytes(_json_bytes(metadata))
    payload_paths["descriptor"].write_bytes(descriptor_bytes)
    inventory_paths = [*payload_paths.values(), release_path]
    inventory = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(inventory_paths, key=lambda entry: entry.name)
    )
    (assets / bootstrap.CHECKSUMS_NAME).write_text(inventory, encoding="ascii")
    for name in bootstrap.ATTESTATION_NAMES.values():
        (attestations / name).write_text("detached proof fixture\n", encoding="ascii")
    for path in [*assets.iterdir(), *attestations.iterdir()]:
        path.chmod(0o644)
    gh = tmp_path / "gh"
    gh.write_bytes(b"test-only gh")
    gh.chmod(0o755)
    return assets, attestations, gh


def _record(candidate: bootstrap.VerifiedPackageCandidate) -> dict[str, Any]:
    material = f"{candidate.package_id}\0{candidate.package_version}\0{candidate.artifact_digest}"
    installation_id = "installation-" + hashlib.sha256(material.encode()).hexdigest()[:32]
    return {
        "record_version": 1,
        "installation_id": installation_id,
        "package_id": candidate.package_id,
        "package_version": candidate.package_version,
        "artifact_digest": candidate.artifact_digest,
        "archive_digest": candidate.archive_digest,
        "capabilities": [candidate.capability],
        "state": "INSTALLED",
        "verification": {
            "verifier": "cy-package-runtime",
            "verified_at_unix_ms": 100,
            "artifact_digest": candidate.artifact_digest,
            "archive_digest": candidate.archive_digest,
            "descriptor_digest": candidate.descriptor_digest,
            "manifest_digest": candidate.manifest_digest,
            "dependency_lock_digest": candidate.dependency_lock_digest,
        },
        "dependencies": {
            "preparer": "cyrene-plugin-python-preparer",
            "prepared_at_unix_ms": 100,
            "lock_digest": candidate.dependency_lock_digest,
            "runtime_digest": _digest(b"runtime"),
            "runtime_executable": "/var/lib/cyrene/runtime/packages/llf/bin/python",
        },
        "installed_at_unix_ms": 100,
    }


@pytest.fixture
def verified_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]]:
    monkeypatch.setattr(bootstrap, "_runtime_group_id", lambda: os.getegid())
    assets, attestations, gh = _write_release(tmp_path)
    real_hash = bootstrap._sha256_file
    monkeypatch.setattr(
        bootstrap,
        "_sha256_file",
        lambda path: (
            f"sha256:{bootstrap.GH_BINARY_SHA256}" if Path(path) == gh else real_hash(Path(path))
        ),
    )
    commands: list[list[str]] = []

    def attest_ok(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "verified", "")

    monkeypatch.setattr(bootstrap.subprocess, "run", attest_ok)
    candidate = bootstrap.verify_signed_package_candidate(assets, attestations, gh)
    assert len(commands) == len(bootstrap.ASSET_NAMES)
    for command in commands:
        assert command[command.index("--repo") + 1] == bootstrap.PACKAGE_SOURCE_REPOSITORY
        assert command[command.index("--signer-workflow") + 1] == bootstrap.PACKAGE_WORKFLOW
        assert command[command.index("--source-ref") + 1] == bootstrap.PACKAGE_SOURCE_REF
        assert command[command.index("--source-digest") + 1] == bootstrap.PACKAGE_SOURCE_COMMIT
        assert command[command.index("--cert-oidc-issuer") + 1] == bootstrap.OIDC_ISSUER
        assert command[command.index("--predicate-type") + 1] == bootstrap.PREDICATE_TYPE
    record = _record(candidate)
    scopes = bootstrap.build_binding_scope_input(candidate, record)
    catalog = {
        "schema_version": 1,
        "generation": 7,
        "sources": [
            {
                "source_id": bootstrap.PACKAGE_SOURCE_ID,
                "uid": 1000,
                "gid": os.getegid(),
                "source_token_sha256": "a" * 64,
                "binding_scopes": scopes[bootstrap.PACKAGE_SOURCE_ID],
            }
        ],
    }
    return candidate, record, catalog


def test_verifies_five_attested_assets_and_preserves_original_descriptor_refs(
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
) -> None:
    candidate, _record_value, _catalog = verified_inputs
    descriptor = bootstrap._load_json(candidate.descriptor_path, "descriptor")
    assert descriptor["integrity"]["signature_ref"] is None
    assert descriptor["provenance"]["attestation_ref"] is None
    assert candidate.source_commit == bootstrap.PACKAGE_SOURCE_COMMIT


def test_pinned_gh_digest_matches_operator_tools_lock() -> None:
    lock_path = (
        Path(__file__).resolve().parents[1]
        / "tooling/acceptance/native-components-v2/operator-tools.lock.json"
    )
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    assert lock["githubCli"]["binarySha256"] == bootstrap.GH_BINARY_SHA256


def test_fixed_cache_derives_local_subject_inventory_from_five_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_root = tmp_path / "plugin-package-bootstrap"
    candidate_root = stage_root / "candidate"
    assets = candidate_root / "assets"
    attestations = candidate_root / "attestations"
    tools = candidate_root / "tools"
    for directory, mode in (
        (stage_root, 0o700),
        (candidate_root, 0o700),
        (assets, 0o700),
        (attestations, 0o700),
        (tools, 0o755),
    ):
        directory.mkdir(mode=mode, parents=True, exist_ok=True)
        directory.chmod(mode)
    for index, name in enumerate(bootstrap.ASSET_NAMES.values()):
        asset = assets / name
        asset.write_bytes(f"signed-payload-{index}".encode())
        asset.chmod(0o600)
    for index, name in enumerate(bootstrap.ATTESTATION_NAMES.values()):
        bundle = attestations / name
        bundle.write_bytes(f"proof-{index}".encode())
        bundle.chmod(0o600)
    (tools / "gh").write_bytes(b"pinned-gh-fixture")
    (tools / "gh").chmod(0o755)

    def write_private(path: Path, content: bytes, mode: int, **_kwargs: Any) -> None:
        path.write_bytes(content)
        path.chmod(mode)

    monkeypatch.setattr(bootstrap, "_write_root_file", write_private)
    monkeypatch.setattr(bootstrap, "PACKAGE_BOOTSTRAP_STAGE_ROOT", stage_root)
    monkeypatch.setattr(bootstrap, "PACKAGE_CANDIDATE_CACHE_ROOT", candidate_root)
    monkeypatch.setattr(bootstrap, "PACKAGE_CANDIDATE_ASSETS", assets)
    monkeypatch.setattr(bootstrap, "PACKAGE_CANDIDATE_ATTESTATIONS", attestations)
    monkeypatch.setattr(bootstrap, "PACKAGE_PINNED_GH", tools / "gh")
    monkeypatch.setattr(bootstrap, "verify_signed_package_candidate", lambda *args: args)

    result = bootstrap.verify_cached_package_candidate()

    inventory = (assets / bootstrap.CHECKSUMS_NAME).read_text(encoding="ascii")
    expected = "".join(
        f"{hashlib.sha256((assets / bootstrap.ASSET_NAMES[key]).read_bytes()).hexdigest()}  {bootstrap.ASSET_NAMES[key]}\n"
        for key in sorted(bootstrap.ASSET_NAMES)
    )
    assert inventory == expected
    assert stat.S_IMODE((assets / bootstrap.CHECKSUMS_NAME).stat().st_mode) == 0o600
    assert result == (assets, attestations, tools / "gh")


def test_package_plan_binds_immutable_inputs_and_exact_confirmation(
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
) -> None:
    candidate, _record_value, catalog = verified_inputs
    plan = bootstrap.build_package_bootstrap_plan(
        candidate, catalog_generation=catalog["generation"], gate_generation=11
    )
    assert plan.component_artifact_digests == {candidate.package_id: candidate.artifact_digest}
    assert plan.material["source_commit"] == bootstrap.PACKAGE_SOURCE_COMMIT
    assert plan.material["preparer_wheel_digest"] == candidate.preparer_wheel_digest
    assert plan.material["gate_generation"] == 11
    assert (
        bootstrap.build_package_bootstrap_plan(
            candidate, catalog_generation=catalog["generation"], gate_generation=12
        ).plan_digest
        != plan.plan_digest
    )
    bootstrap.validate_package_bootstrap_confirmation(
        plan,
        {"plan_id": plan.plan_id, "plan_digest": plan.plan_digest, "confirmed": True},
    )
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError, match="confirmation"):
        bootstrap.validate_package_bootstrap_confirmation(
            plan,
            {"plan_id": plan.plan_id, "plan_digest": _digest(b"other"), "confirmed": True},
        )


@pytest.mark.parametrize("field", ["source", "channel", "asset_digest"])
def test_rejects_release_identity_or_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    assets, attestations, gh = _write_release(tmp_path)
    release_path = assets / bootstrap.ASSET_NAMES["package_release"]
    metadata = json.loads(release_path.read_text(encoding="utf-8"))
    if field == "source":
        metadata["source"]["commit"] = "0" * 40
    elif field == "channel":
        metadata["channel"] = "stable"
    else:
        (assets / bootstrap.ASSET_NAMES["archive"]).write_bytes(b"changed bytes")
    release_path.write_bytes(_json_bytes(metadata))
    entries = [path for path in assets.iterdir() if path.name != bootstrap.CHECKSUMS_NAME]
    checksums = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(entries, key=lambda item: item.name)
    )
    (assets / bootstrap.CHECKSUMS_NAME).write_text(checksums, encoding="ascii")
    real_hash = bootstrap._sha256_file
    monkeypatch.setattr(
        bootstrap,
        "_sha256_file",
        lambda path: (
            f"sha256:{bootstrap.GH_BINARY_SHA256}" if Path(path) == gh else real_hash(Path(path))
        ),
    )
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError):
        bootstrap.verify_signed_package_candidate(assets, attestations, gh)


def test_rejects_failed_official_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets, attestations, gh = _write_release(tmp_path)
    real_hash = bootstrap._sha256_file
    monkeypatch.setattr(
        bootstrap,
        "_sha256_file",
        lambda path: (
            f"sha256:{bootstrap.GH_BINARY_SHA256}" if Path(path) == gh else real_hash(Path(path))
        ),
    )
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "invalid"),
    )
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError, match="detached attestation"):
        bootstrap.verify_signed_package_candidate(assets, attestations, gh)


def test_actual_install_receipt_derives_only_fixed_binding_scope(
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
) -> None:
    candidate, record, _catalog = verified_inputs
    assert bootstrap.build_binding_scope_input(candidate, record) == {
        bootstrap.PACKAGE_SOURCE_ID: [
            {
                "binding_id": bootstrap.PACKAGE_BINDING_ID,
                "package_id": bootstrap.PACKAGE_ID,
                "installation_ids": [record["installation_id"]],
                "operations": ["activate", "deactivate", "recover"],
            }
        ]
    }


@pytest.mark.parametrize(
    "change",
    ["installation", "package", "state", "artifact", "descriptor", "runtime_path"],
)
def test_rejects_receipts_not_bound_to_verified_bytes(
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
    change: str,
) -> None:
    candidate, record, _catalog = verified_inputs
    changed = json.loads(json.dumps(record))
    if change == "installation":
        changed["installation_id"] = "installation-" + "0" * 32
    elif change == "package":
        changed["package_id"] = "other.package"
    elif change == "state":
        changed["state"] = "PENDING"
    elif change == "artifact":
        changed["artifact_digest"] = _digest(b"wrong")
    elif change == "descriptor":
        changed["verification"]["descriptor_digest"] = _digest(b"wrong")
    else:
        changed["dependencies"]["runtime_executable"] = "relative/python"
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError, match="receipt"):
        bootstrap.validate_installation_record(candidate, changed)


def test_derives_minimal_policy_from_current_generation_and_receipt(
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
) -> None:
    candidate, record, catalog = verified_inputs
    policy = bootstrap.build_runtime_source_policy(candidate, record, catalog)
    assert policy["generation"] == catalog["generation"]
    assert set(policy) == {"schema_version", "generation", "sources"}
    source = policy["sources"][0]
    assert set(source) == {"source_id", "uid", "gid", "source_token_sha256", "bindings"}
    assert source["bindings"][0]["operations"] == [
        "activate",
        "deactivate",
        "get_installation",
        "recover_binding",
        "runtime_status",
    ]
    assert catalog["sources"][0]["binding_scopes"][0]["operations"] == [
        "activate",
        "deactivate",
        "recover",
    ]
    assert "operator_token" not in json.dumps(policy)
    assert bootstrap.validate_runtime_source_policy(policy, candidate, record, catalog) == policy


def test_catalog_update_preserves_other_sources_and_adds_actual_installation_scope(
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
) -> None:
    candidate, record, catalog = verified_inputs
    existing = {
        "binding_id": "yield.other.owner",
        "package_id": "cyrene.other.package",
        "installation_ids": ["installation-" + "2" * 32],
        "operations": ["activate"],
    }
    unrelated = {
        "source_id": "cyrene-z",
        "uid": 1001,
        "gid": os.getegid(),
        "source_token_sha256": "b" * 64,
        "binding_scopes": [
            {
                "binding_id": "z.package.primary",
                "package_id": "cyrene.z.package",
                "installation_ids": ["installation-" + "3" * 32],
                "operations": ["deactivate"],
            }
        ],
    }
    catalog["sources"][0]["binding_scopes"] = [existing]
    catalog["sources"].append(unrelated)
    update = bootstrap.build_activity_catalog_update(candidate, record, catalog)

    assert update.changed is True
    assert update.expected_generation == catalog["generation"] + 1
    assert update.source_arguments == (
        "--source",
        "cyrene-yield=1000:" + str(os.getegid()),
        "--source",
        "cyrene-z=1001:" + str(os.getegid()),
    )
    assert update.binding_scopes["cyrene-yield"] == sorted(
        [existing, bootstrap.build_binding_scope_input(candidate, record)["cyrene-yield"][0]],
        key=lambda scope: scope["binding_id"],
    )
    assert update.binding_scopes["cyrene-z"] == unrelated["binding_scopes"]
    readback = json.loads(json.dumps(catalog))
    readback["generation"] = update.expected_generation
    for source in readback["sources"]:
        source["binding_scopes"] = update.binding_scopes[source["source_id"]]
    command_result = {
        "schema_version": 1,
        "generation": update.expected_generation,
        "sources": [
            {
                "source_id": source_id,
                "token_file": f"/etc/cyrene/runtime-activity-source-tokens/{source_id}.token",
                "binding_scope_count": len(update.binding_scopes[source_id]),
            }
            for source_id in sorted(update.source_identity)
        ],
    }
    assert (
        bootstrap.validate_activity_catalog_readback(readback, update, command_result) == readback
    )
    readback["sources"][0]["source_token_sha256"] = "c" * 64
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError, match="identity or scope"):
        bootstrap.validate_activity_catalog_readback(readback, update, command_result)


def test_catalog_update_refuses_rebinding_owner_binding_to_another_installation(
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
) -> None:
    candidate, record, catalog = verified_inputs
    catalog["sources"][0]["binding_scopes"] = [
        {
            "binding_id": bootstrap.PACKAGE_BINDING_ID,
            "package_id": bootstrap.PACKAGE_ID,
            "installation_ids": ["installation-" + "9" * 32],
            "operations": list(bootstrap.PACKAGE_POLICY_OPERATIONS),
        }
    ]
    with pytest.raises(
        bootstrap.PackageRuntimeBootstrapError, match="different package installation"
    ):
        bootstrap.build_activity_catalog_update(candidate, record, catalog)


def test_init_catalog_command_keeps_complete_sources_and_uses_private_proof_inputs(
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    candidate, record, catalog = verified_inputs
    catalog["sources"].append(
        {
            "source_id": "cyrene-z",
            "uid": 1001,
            "gid": os.getegid(),
            "source_token_sha256": "b" * 64,
        }
    )
    update = bootstrap.build_activity_catalog_update(candidate, record, catalog)
    stage = tmp_path / "staging"
    stage.mkdir()
    proof = stage / "maintenance-proof.json"
    scopes = stage / "binding-scopes.json"
    proof.touch()
    scopes.touch()
    monkeypatch.setattr(bootstrap, "PACKAGE_BOOTSTRAP_STAGE_ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "_maintenance_group_id", lambda: 998)
    monkeypatch.setattr(
        bootstrap,
        "_validate_private_stage_json",
        lambda path: update.binding_scopes if path == scopes else {"request_id": "r"},
    )
    seen: list[list[str]] = []

    def fake_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        assert kwargs["timeout"] == 120
        result = {
            "schema_version": 1,
            "generation": update.expected_generation,
            "sources": [
                {
                    "source_id": source_id,
                    "token_file": f"/etc/cyrene/runtime-activity-source-tokens/{source_id}.token",
                    "binding_scope_count": len(update.binding_scopes[source_id]),
                }
                for source_id in sorted(update.source_identity)
            ],
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

    result = bootstrap.run_activity_catalog_update(
        update, proof_path=proof, scopes_path=scopes, runner=fake_runner
    )
    assert result["generation"] == update.expected_generation
    assert seen[0].count("--source") == len(update.source_identity)
    assert seen[0][seen[0].index("--maintenance-proof-file") + 1] == str(proof)
    assert "opaque-test-token" not in " ".join(seen[0])


def test_offline_install_request_and_response_are_bound_to_the_real_hold(
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
    tmp_path: Path,
) -> None:
    candidate, record, _catalog = verified_inputs
    maintenance = {
        "transaction_id": "tx-1",
        "maintenance_token": "opaque-test-token",
        "target_kind": "PACKAGE_ONLY",
        "plan_id": "plan-123",
        "plan_digest": _digest(b"plan"),
        "component_artifact_digests": {candidate.package_id: candidate.artifact_digest},
        "expected_gate_generation": 9,
        "expected_catalog_generation": 7,
    }
    request = bootstrap.build_offline_install_input(
        candidate,
        request_id="package-bootstrap-1",
        descriptor_path=tmp_path / "descriptor.json",
        archive_path=tmp_path / "archive.zip",
        maintenance=maintenance,
    )
    assert set(request) == {"schema_version", "request_id", "maintenance", "candidate"}
    assert request["candidate"]["component_id"] == candidate.package_id
    result = {
        "request_id": request["request_id"],
        "ok": True,
        "result": {
            "maintenance": {
                "transaction_id": "tx-1",
                "target_kind": "PACKAGE_ONLY",
                "plan_id": "plan-123",
                "plan_digest": _digest(b"plan"),
                "component_artifact_digests": {candidate.package_id: candidate.artifact_digest},
                "component_id": candidate.package_id,
                "artifact_digest": candidate.artifact_digest,
                "expected_gate_generation": 9,
                "expected_catalog_generation": 7,
            },
            "catalog_generation": 7,
            "gate_generation": 9,
            "installation": record,
        },
    }
    assert bootstrap.validate_offline_install_result(candidate, request, result) == record


def test_offline_install_request_rejects_unbound_package_digest(
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
    tmp_path: Path,
) -> None:
    candidate, _record_value, _catalog = verified_inputs
    maintenance = {
        "transaction_id": "tx-1",
        "maintenance_token": "opaque-test-token",
        "target_kind": "PACKAGE_ONLY",
        "plan_id": "plan-123",
        "plan_digest": _digest(b"plan"),
        "component_artifact_digests": {candidate.package_id: _digest(b"different")},
        "expected_gate_generation": 9,
        "expected_catalog_generation": 7,
    }
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError):
        bootstrap.build_offline_install_input(
            candidate,
            request_id="package-bootstrap-1",
            descriptor_path=tmp_path / "descriptor.json",
            archive_path=tmp_path / "archive.zip",
            maintenance=maintenance,
        )


def test_staging_preserves_all_signed_inputs_and_bundles_in_private_cache(
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, _record_value, _catalog = verified_inputs
    stage_root = tmp_path / "plugin-package-bootstrap"

    def ensure_directory(
        path: Path, mode: int, *, parent: Path | None = None, **_kwargs: Any
    ) -> None:
        if parent is not None:
            parent.mkdir(mode=0o700, exist_ok=True)
        path.mkdir(mode=mode, exist_ok=True)
        path.chmod(mode)

    def write_private(path: Path, content: bytes, mode: int, **_kwargs: Any) -> None:
        path.write_bytes(content)
        path.chmod(mode)

    def read_source(path: Path, expected: str, limit: int) -> bytes:
        content = path.read_bytes()
        if len(content) > limit or "sha256:" + hashlib.sha256(content).hexdigest() != expected:
            raise bootstrap.PackageRuntimeBootstrapError("digest mismatch")
        return content

    monkeypatch.setattr(bootstrap, "_effective_uid", lambda: 0)
    monkeypatch.setattr(bootstrap, "_ensure_root_directory", ensure_directory)
    monkeypatch.setattr(bootstrap, "_write_root_file", write_private)
    monkeypatch.setattr(bootstrap, "_safe_source_bytes", read_source)
    monkeypatch.setattr(bootstrap, "PACKAGE_BOOTSTRAP_STAGE_ROOT", stage_root)
    request_id = "cyrene-update-plan-" + "a" * 32
    request_directory, request_path = bootstrap.stage_offline_install_request(
        candidate,
        request_id=request_id,
        maintenance={
            "transaction_id": request_id,
            "maintenance_token": "opaque-token",
            "target_kind": "PACKAGE_ONLY",
            "plan_id": "plan-" + "a" * 32,
            "plan_digest": _digest(b"plan"),
            "component_artifact_digests": {candidate.package_id: candidate.artifact_digest},
            "expected_gate_generation": 11,
            "expected_catalog_generation": 9,
        },
        staging_root=stage_root,
    )

    evidence_assets = request_directory / "evidence/assets"
    evidence_attestations = request_directory / "evidence/attestations"
    assert request_path.parent == request_directory
    assert stat.S_IMODE(request_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600
    assert {path.name for path in evidence_assets.iterdir()} == {
        *bootstrap.ASSET_NAMES.values(),
        bootstrap.CHECKSUMS_NAME,
    }
    assert {path.name for path in evidence_attestations.iterdir()} == set(
        bootstrap.ATTESTATION_NAMES.values()
    )
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in evidence_assets.iterdir())
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600 for path in evidence_attestations.iterdir()
    )


def test_rejects_malformed_scopes_even_on_an_unrelated_catalog_source(
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
) -> None:
    candidate, record, catalog = verified_inputs
    changed = json.loads(json.dumps(catalog))
    changed["sources"].append(
        {
            "source_id": "cyrene-z",
            "uid": 1001,
            "gid": os.getegid(),
            "source_token_sha256": "b" * 64,
            "binding_scopes": [{}],
        }
    )
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError, match="binding scope"):
        bootstrap.build_runtime_source_policy(candidate, record, changed)


@pytest.mark.parametrize(
    "change",
    ["generation", "gid", "token_hash", "scope", "source_order", "bool_version"],
)
def test_rejects_catalog_identity_or_scope_drift(
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
    change: str,
) -> None:
    candidate, record, catalog = verified_inputs
    policy = bootstrap.build_runtime_source_policy(candidate, record, catalog)
    changed = json.loads(json.dumps(catalog))
    if change == "generation":
        changed["generation"] += 1
    elif change == "gid":
        changed["sources"][0]["gid"] += 1
    elif change == "token_hash":
        changed["sources"][0]["source_token_sha256"] = "not-a-digest"
    elif change == "scope":
        changed["sources"][0]["binding_scopes"][0]["installation_ids"] = [
            "installation-" + "0" * 32
        ]
    elif change == "source_order":
        changed["sources"].append(dict(changed["sources"][0]))
    else:
        changed["schema_version"] = True
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError):
        if change == "generation":
            bootstrap.validate_runtime_source_policy(policy, candidate, record, changed)
        else:
            bootstrap.build_runtime_source_policy(candidate, record, changed)


def test_policy_writer_sets_exact_owner_mode_is_idempotent_and_refuses_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
) -> None:
    candidate, record, catalog = verified_inputs
    policy = bootstrap.build_runtime_source_policy(candidate, record, catalog)
    root = tmp_path / "root"
    parent = root / "etc/cyrene"
    parent.mkdir(parents=True)
    parent.chmod(0o755)
    monkeypatch.setattr(bootstrap, "_require_root", lambda: None)
    monkeypatch.setattr(bootstrap, "_runtime_group_id", lambda: os.getegid())
    digest = bootstrap.write_runtime_source_policy(policy, root=root)
    path = root / bootstrap.PACKAGE_POLICY_RELATIVE_PATH
    info = path.stat()
    assert stat.S_IMODE(info.st_mode) == 0o440
    assert info.st_uid == os.geteuid()
    assert info.st_gid == os.getegid()
    assert bootstrap.read_runtime_source_policy(root=root) == policy
    assert bootstrap.write_runtime_source_policy(policy, root=root) == digest
    changed = json.loads(json.dumps(policy))
    changed["generation"] += 1
    with pytest.raises(
        bootstrap.PackageRuntimeBootstrapError, match="different Package Runtime policy"
    ):
        bootstrap.write_runtime_source_policy(changed, root=root)


def test_policy_reader_rejects_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verified_inputs: tuple[bootstrap.VerifiedPackageCandidate, dict[str, Any], dict[str, Any]],
) -> None:
    candidate, record, catalog = verified_inputs
    policy = bootstrap.build_runtime_source_policy(candidate, record, catalog)
    root = tmp_path / "root"
    parent = root / "etc/cyrene"
    parent.mkdir(parents=True)
    parent.chmod(0o755)
    monkeypatch.setattr(bootstrap, "_runtime_group_id", lambda: os.getegid())
    monkeypatch.setattr(bootstrap, "_require_root", lambda: None)
    path = root / bootstrap.PACKAGE_POLICY_RELATIVE_PATH
    target = parent / "real-policy"
    target.write_bytes(_json_bytes(policy))
    target.chmod(0o440)
    path.symlink_to(target.name)
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError, match="missing or unsafe"):
        bootstrap.read_runtime_source_policy(root=root)
