"""Fixed-input tests for the read-only production control preflight."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = WORKSPACE_ROOT / "tooling/acceptance/native-components-v2/control_initialize.py"
SPEC = importlib.util.spec_from_file_location("native_control_initialize_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = control
SPEC.loader.exec_module(control)


def _write(path: Path, data: bytes, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def _args(tmp_path: Path) -> tuple[object, Path, dict[str, Path]]:
    uid = os.geteuid()
    root = tmp_path / "target"
    for path in (root / "etc", root / "var", root / "var/lib"):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o755)
    root.chmod(0o755)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    secret_paths = {
        "authority_db": _write(private / "authority-db-url", b"postgresql://redacted\n"),
        "signing_key": _write(private / "authority-signing-key", b"not read by this test\n"),
        "tls_cert": _write(private / "authority-cert.pem", b"public certificate\n"),
        "tls_key": _write(private / "authority-tls-key", b"not read by this test\n"),
        "client_ca": _write(private / "authority-client-ca.pem", b"public CA\n"),
        "ca_key": _write(private / "device-ca-key", b"not read by this test\n"),
        "ca_cert": _write(private / "device-ca-cert.pem", b"public CA\n"),
    }
    for path in secret_paths.values():
        os.chown(path, uid, os.getegid())

    authority_env = _write(
        tmp_path / "authority.env",
        (
            "CYRENE_AUTHORITY_AAD_TENANT_ID=11111111-2222-3333-4444-555555555555\n"
            "CYRENE_AUTHORITY_AAD_AUDIENCE=api://cyrene-workspace\n"
            "CYRENE_AUTHORITY_SIGNING_KEY_ID=authority-key-2026\n"
            f"CYRENE_AUTHORITY_DATABASE_URL_FILE={secret_paths['authority_db']}\n"
            f"CYRENE_AUTHORITY_SIGNING_KEY_FILE={secret_paths['signing_key']}\n"
            f"CYRENE_AUTHORITY_TLS_CERT_FILE={secret_paths['tls_cert']}\n"
            f"CYRENE_AUTHORITY_TLS_KEY_FILE={secret_paths['tls_key']}\n"
            f"CYRENE_AUTHORITY_TLS_CLIENT_CA_FILE={secret_paths['client_ca']}\n"
            "CYRENE_WORKSPACE_AUTHORITY_TRUST_CONFIG=/etc/cyrene-workspace-authority/trust.json\n"
            "CYRENE_AUTHORITY_INITIAL_ARTIFACT_ID_FILE=/etc/cyrene-workspace-authority/initial-artifact-id\n"
        ).encode(),
    )
    identity_env = _write(
        tmp_path / "authority-identity.env",
        b"CYRENE_AUTHORITY_AAD_TENANT_ID=11111111-2222-3333-4444-555555555555\n"
        b"CYRENE_AUTHORITY_AAD_AUDIENCE=api://cyrene-workspace\n",
    )
    migration_values = "".join(
        f"{key}=postgresql://migration-secret-{index}\n"
        for index, key in enumerate(sorted(control.MIGRATION_DATABASE_KEYS))
    )
    migration_env = _write(tmp_path / "migrations.env", migration_values.encode())
    ca_env = _write(
        tmp_path / "device-ca.env",
        (
            "CYRENE_WORKSPACE_DEVICE_CA_DATABASE_URL=postgresql://ca-secret-value\n"
            f"CYRENE_WORKSPACE_DEVICE_CA_SIGNING_KEY_FILE={secret_paths['ca_key']}\n"
            f"CYRENE_WORKSPACE_DEVICE_CA_CERTIFICATE_FILE={secret_paths['ca_cert']}\n"
            "CYRENE_WORKSPACE_DEVICE_CA_ISSUER_ID=workspace-device-ca-1\n"
        ).encode(),
    )
    for path in (authority_env, identity_env, migration_env, ca_env):
        os.chown(path, uid, os.getegid())

    catalog = tmp_path / "component-catalog-bootstrap-v1.json"
    catalog.write_bytes(
        (WORKSPACE_ROOT / "packaging/component-catalog-bootstrap-v1.json").read_bytes()
    )
    catalog.chmod(0o644)
    values = {
        "catalog": catalog,
        "channel": "stable",
        "index": tmp_path / "index.json",
        "index_attestation": tmp_path / "index.bundle.json",
        "manifest": tmp_path / "manifest.json",
        "artifact": tmp_path / "artifact.tar.zst",
        "artifact_attestation": tmp_path / "artifact.bundle.json",
        "control_ubuntu": "24.04",
        "authority_env": authority_env,
        "identity_env": identity_env,
        "migration_env": migration_env,
        "device_ca_env": ca_env,
        "tenant_id": "11111111-2222-3333-4444-555555555555",
        "audience": "api://cyrene-workspace",
        "trust_config": "/etc/cyrene-workspace-authority/trust.json",
        "trust_config_sha256": "sha256:" + "a" * 64,
        "initial_artifact_id": "/etc/cyrene-workspace-authority/initial-artifact-id",
        "service_uid": uid,
    }
    args = argparse_namespace(values)
    return args, root, secret_paths


def argparse_namespace(values: dict[str, object]) -> object:
    return type("Args", (), values)()


def _valid_bundle(_args: object, *, updater_module: object, runner: object) -> dict[str, object]:
    return {
        "status": "PASS",
        "componentId": control.COMPONENT_ID,
        "artifactId": "sha256:" + "b" * 64,
        "version": "1.2.3",
        "channel": "stable",
        "source": {
            "repository": "DoHorizon-AI/Cyrene-Workspace",
            "ref": "refs/heads/main",
            "commit": "c" * 40,
        },
        "catalogDigest": control.TRUSTED_CATALOG_DIGEST,
        "indexDigest": "sha256:" + "d" * 64,
        "manifestDigest": "sha256:" + "e" * 64,
        "artifactDigest": "sha256:" + "b" * 64,
        "proofSchemaVersion": 1,
        "signatureVerification": "PASS (fixed mock)",
    }


def test_missing_signed_input_is_rejected_without_network_or_state_change(tmp_path: Path) -> None:
    args, _root, _paths = _args(tmp_path)
    with pytest.raises(control.ControlInitializationError, match="release index"):
        control._bundle_validation(
            args,
            updater_module=control._load_updater(),
            runner=lambda *_a, **_k: pytest.fail("external command must not run"),
        )


def test_control_preflight_pin_matches_the_canonical_c10_catalog() -> None:
    catalog = WORKSPACE_ROOT / "packaging/component-catalog-bootstrap-v1.json"
    expected = "sha256:" + hashlib.sha256(catalog.read_bytes()).hexdigest()

    assert control.TRUSTED_CATALOG_DIGEST == expected
    assert control.TRUSTED_CATALOG_DIGEST == control._load_updater().TRUSTED_CATALOG_DIGEST


def test_wrong_catalog_digest_and_wrong_control_platform_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, root, _paths = _args(tmp_path)
    bad_catalog = _write(tmp_path / "catalog.json", b'{"schemaVersion":1}\n')
    args.catalog = bad_catalog
    with pytest.raises(control.ControlInitializationError, match="compiled authority digest"):
        control._bundle_validation(
            args,
            updater_module=control._load_updater(),
            runner=lambda *_a, **_k: pytest.fail("no command expected"),
        )

    args, root, _paths = _args(tmp_path / "platform")
    args.control_ubuntu = "20.04"
    monkeypatch.setattr(control, "_bundle_validation", _valid_bundle)
    with pytest.raises(control.ControlInitializationError, match="Ubuntu 22.04 or 24.04"):
        control.build_plan(args, filesystem_root=root, uid=os.geteuid())


def _emitter_index(
    updater_module: object,
    updater: object,
    component: dict[str, object],
    target: dict[str, object],
    entries: list[dict[str, object]],
) -> dict[str, object]:
    publisher = updater.publishers[component["publisher"]]
    commit = "a" * 40
    index: dict[str, object] = {
        "schemaVersion": 1,
        "repository": publisher["repository"],
        "channel": "stable",
        "generatedAt": "2026-01-01T00:00:00Z",
        "source": {
            "repository": f"https://github.com/{publisher['repository']}",
            "ref": "refs/heads/main",
            "commit": commit,
        },
        "provenance": {
            "attestation": {
                "kind": "github-artifact-attestation",
                "subjectName": publisher["releaseDiscovery"]["indexAssetName"],
                "repository": publisher["repository"],
                "workflow": publisher["workflow"],
                "predicateType": "https://slsa.dev/provenance/v1",
                "run": {
                    "id": "123456",
                    "attempt": 1,
                    "url": f"https://github.com/{publisher['repository']}/actions/runs/123456/attempts/1",
                },
            }
        },
        # This is the real build_data_bundle_release.py shape: `target` is the
        # complete manifest target object; there is no targetId field here.
        "releases": entries,
        "compatibilityGroups": [],
    }
    index["indexDigest"] = updater_module._digest_json(index, "indexDigest")
    return index


def test_bundle_target_merges_the_component_artifact_declaration() -> None:
    updater_module = control._load_updater()
    updater = updater_module.ComponentUpdater(
        catalog_path=WORKSPACE_ROOT / "packaging/component-catalog-bootstrap-v1.json",
        trusted_catalog_digest=control.TRUSTED_CATALOG_DIGEST,
        load_active_catalog=False,
    )
    component = updater.components[control.COMPONENT_ID]
    catalog_target = updater.targets[control.DATA_BUNDLE_TARGET]

    assert "artifactKind" not in catalog_target
    assert control._bundle_target(updater, component) == {
        **catalog_target,
        "artifactKind": "data-bundle",
    }

    component["targets"] = [
        {
            "targetId": control.DATA_BUNDLE_TARGET,
            "artifactKind": "data-bundle",
            "support": "contract-only",
        }
    ]
    with pytest.raises(control.ControlInitializationError, match="uniquely authorize"):
        control._bundle_target(updater, component)


def test_pretty_manifest_bytes_use_the_signed_canonical_digest() -> None:
    updater_module = control._load_updater()
    manifest: dict[str, object] = {
        "componentId": control.COMPONENT_ID,
        "target": {"architecture": "x86_64", "os": "linux", "runtime": "cyrene-authority-data"},
        "version": "1.2.3",
    }
    manifest["manifestDigest"] = updater_module._digest_json(manifest, "manifestDigest")
    entry = {"manifestDigest": manifest["manifestDigest"]}
    raw_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()

    assert "sha256:" + hashlib.sha256(raw_bytes).hexdigest() != manifest["manifestDigest"]
    control._validate_manifest_index_digest(updater_module, manifest, entry)

    changed = {**manifest, "version": "1.2.4"}
    with pytest.raises(control.ControlInitializationError, match="canonical digest"):
        control._validate_manifest_index_digest(updater_module, changed, entry)

    changed["manifestDigest"] = updater_module._digest_json(changed, "manifestDigest")
    with pytest.raises(control.ControlInitializationError, match="canonical digest"):
        control._validate_manifest_index_digest(updater_module, changed, entry)


def test_emitter_target_selects_unique_full_target_and_reaches_manifest_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater_module = control._load_updater()
    updater = updater_module.ComponentUpdater(
        catalog_path=WORKSPACE_ROOT / "packaging/component-catalog-bootstrap-v1.json",
        trusted_catalog_digest=control.TRUSTED_CATALOG_DIGEST,
        load_active_catalog=False,
    )
    component = updater.components[control.COMPONENT_ID]
    target = updater.targets[control.DATA_BUNDLE_TARGET]
    publisher = updater.publishers[component["publisher"]]
    entry = {
        "componentId": control.COMPONENT_ID,
        "version": "1.2.3",
        "target": target["target"],
        "manifestUri": "https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/stable/manifest.json",
        "manifestDigest": "sha256:" + "b" * 64,
    }
    index = _emitter_index(updater_module, updater, component, target, [entry])
    updater._validate_index(
        index,
        publisher,
        "stable",
        {
            "tag_name": (updater._component_release_tag_prefix(component, "stable") or "stable-")
            + index["source"]["commit"]
        },
        component,
    )
    validation_calls: list[tuple[dict[str, object], dict[str, object]]] = []
    monkeypatch.setattr(
        updater,
        "_validate_manifest",
        lambda manifest, received_entry, *_args: validation_calls.append(
            (manifest, received_entry)
        ),
    )
    manifest = {"target": target["target"]}

    selected = control._validate_indexed_manifest(
        updater, manifest, index, component, target, publisher, "stable"
    )

    assert selected == entry
    assert validation_calls == [(manifest, entry)]


@pytest.mark.parametrize("case", ["duplicate", "wrong-target"])
def test_emitter_target_duplicate_or_mismatch_is_rejected(case: str) -> None:
    updater_module = control._load_updater()
    updater = updater_module.ComponentUpdater(
        catalog_path=WORKSPACE_ROOT / "packaging/component-catalog-bootstrap-v1.json",
        trusted_catalog_digest=control.TRUSTED_CATALOG_DIGEST,
        load_active_catalog=False,
    )
    component = updater.components[control.COMPONENT_ID]
    target = updater.targets[control.DATA_BUNDLE_TARGET]
    valid = {
        "componentId": control.COMPONENT_ID,
        "version": "1.2.3",
        "target": target["target"],
        "manifestUri": "https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/stable/manifest.json",
        "manifestDigest": "sha256:" + "b" * 64,
    }
    entries = (
        [valid, dict(valid)]
        if case == "duplicate"
        else [
            {
                **valid,
                "target": {**target["target"], "osVersion": "24.04"},
                "targetId": control.DATA_BUNDLE_TARGET,
            }
        ]
    )
    index = _emitter_index(updater_module, updater, component, target, entries)
    publisher = updater.publishers[component["publisher"]]
    updater._validate_index(
        index,
        publisher,
        "stable",
        {
            "tag_name": (updater._component_release_tag_prefix(component, "stable") or "stable-")
            + index["source"]["commit"]
        },
        component,
    )

    with pytest.raises(control.ControlInitializationError, match="exactly one.*full target"):
        control._select_bundle_release_entry(index, component, target)


@pytest.mark.parametrize("unsafe", ["symlink", "wide-mode"])
def test_unsafe_input_file_is_rejected(tmp_path: Path, unsafe: str) -> None:
    target = tmp_path / "input"
    if unsafe == "symlink":
        real = _write(tmp_path / "real", b"index")
        target.symlink_to(real)
    else:
        _write(target, b"index", 0o666)
    with pytest.raises(control.ControlInitializationError, match="regular file"):
        control._regular_input(target, "index")


def test_existing_mismatched_initial_selector_refuses_and_keeps_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, root, _paths = _args(tmp_path)
    selector = root / "etc/cyrene-workspace-authority/initial-artifact-id"
    selector.parent.mkdir(mode=0o700)
    _write(selector, ("sha256:" + "0" * 64 + "\n").encode())
    os.chown(selector.parent, os.geteuid(), os.getegid())
    os.chown(selector, os.geteuid(), os.getegid())
    monkeypatch.setattr(control, "_bundle_validation", _valid_bundle)
    with pytest.raises(control.ControlInitializationError, match="does not match"):
        control.build_plan(args, filesystem_root=root, uid=os.geteuid())
    assert selector.read_text() == "sha256:" + "0" * 64 + "\n"


def test_plan_redacts_secret_environment_values_and_reports_no_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, root, _paths = _args(tmp_path)
    monkeypatch.setattr(control, "_bundle_validation", _valid_bundle)
    plan = control.build_plan(args, filesystem_root=root, uid=os.geteuid())
    serialized = json.dumps(plan)
    assert plan["mode"] == "read-only-plan"
    assert plan["status"] == "PLAN_ONLY"
    assert plan["mutationsPerformed"] == []
    assert plan["execution"]["thisModuleProvisionedAnything"] is False
    assert plan["authority"]["privateContentsReadOrEmitted"] is False
    assert "migration-secret-" not in serialized
    assert "ca-secret-value" not in serialized
    assert "postgresql://redacted" not in serialized


def test_trust_directory_symlink_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, root, _paths = _args(tmp_path)
    authority_path = root / "etc/cyrene-workspace-authority"
    authority_path.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(control, "_bundle_validation", _valid_bundle)
    with pytest.raises(control.ControlInitializationError, match="protected directory is unsafe"):
        control.build_plan(args, filesystem_root=root, uid=os.geteuid())
