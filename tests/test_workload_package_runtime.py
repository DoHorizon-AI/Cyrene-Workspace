"""Contract tests for Workspace's Package Runtime lifecycle adapter."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "packaging" / "workload_package_runtime.py"
SPEC = importlib.util.spec_from_file_location("cyrene_workload_package_runtime_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _raw_digest(label: str) -> str:
    return _digest(label).removeprefix("sha256:")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _source_policy(*, standalone: bool = False) -> dict[str, Any]:
    if standalone:
        return {
            "mode": "standaloneOperator",
            "productComponentIds": [],
            "productSources": [],
            "operations": list(runtime.SOURCE_OPERATIONS),
            "sourceId": "cyrene-plugin-standalone-operator",
        }
    return {
        "mode": "actualProduct",
        "productComponentIds": ["cyrene-catalyst"],
        "productSources": [{"componentId": "cyrene-catalyst", "sourceId": "cyrene-catalyst"}],
        "operations": list(runtime.SOURCE_OPERATIONS),
    }


def _package_row(
    *, standalone: bool = False, binding_id: str = "plugin-a-catalyst"
) -> dict[str, Any]:
    return {
        "componentId": "cyrene-tools-plugin-a",
        "packageId": "org.example.plugin-a",
        "version": "1.2.3",
        "capabilityId": "org.example.plugin-a",
        "bindingId": binding_id,
        "sourcePolicy": _source_policy(standalone=standalone),
    }


def _installation_record(
    *,
    package_id: str = "org.example.plugin-a",
    version: str = "1.2.3",
    artifact_digest: str | None = None,
    archive_digest: str | None = None,
    installation_id: str | None = None,
) -> dict[str, Any]:
    artifact_digest = artifact_digest or _digest(f"artifact-{version}")
    archive_digest = archive_digest or _digest(f"archive-{version}")
    installation_id = (
        installation_id
        or "installation-"
        + hashlib.sha256(f"{package_id}\0{version}\0{artifact_digest}".encode()).hexdigest()[:32]
    )
    verification = {
        "verifier": "workspace-offline-package-verifier-v1",
        "verified_at_unix_ms": 100,
        "artifact_digest": artifact_digest,
        "archive_digest": archive_digest,
        "descriptor_digest": _digest(f"descriptor-{version}"),
        "manifest_digest": _digest(f"manifest-{version}"),
        "dependency_lock_digest": _digest(f"lock-{version}"),
    }
    return {
        "record_version": 1,
        "installation_id": installation_id,
        "package_id": package_id,
        "package_version": version,
        "artifact_digest": artifact_digest,
        "archive_digest": archive_digest,
        "capabilities": [package_id],
        "state": "INSTALLED",
        "verification": verification,
        "dependencies": {
            "preparer": "cyrene-plugin-python-preparer",
            "prepared_at_unix_ms": 101,
            "lock_digest": verification["dependency_lock_digest"],
            "runtime_digest": _digest(f"runtime-{version}"),
            "runtime_executable": "/opt/cyrene/runtime/prepared-runtime",
        },
        "installed_at_unix_ms": 102,
    }


def _broker_scope(binding_id: str, package_id: str, installation_ids: list[str]) -> dict[str, Any]:
    return {
        "binding_id": binding_id,
        "package_id": package_id,
        "installation_ids": sorted(set(installation_ids)),
        "operations": list(runtime.BROKER_OPERATIONS),
    }


def _runtime_binding(
    binding_id: str, package_id: str, installation_ids: list[str]
) -> dict[str, Any]:
    return {
        "binding_id": binding_id,
        "package_id": package_id,
        "installation_ids": sorted(set(installation_ids)),
        "operations": list(runtime.RUNTIME_OPERATIONS),
    }


def _principal(source_id: str, *, uid: int, gid: int, token_path: Path) -> dict[str, Any]:
    return {"uid": uid, "gid": gid, "tokenPath": token_path}


def _activity_catalog(rows: list[dict[str, Any]], *, generation: int = 5) -> dict[str, Any]:
    return {"schema_version": 1, "generation": generation, "sources": rows}


def _runtime_policy(sources: list[dict[str, Any]], *, generation: int = 5) -> dict[str, Any]:
    return {"schema_version": 1, "generation": generation, "sources": sources}


def _hint(record: dict[str, Any], *, component_id: str = "cyrene-tools-plugin-a") -> dict[str, Any]:
    verification = record["verification"]
    return {
        "componentId": component_id,
        "packageId": record["package_id"],
        "installed": True,
        "installationId": record["installation_id"],
        "version": record["package_version"],
        "releaseId": "plugin-release-immutable-abc123",
        "manifestUri": "https://example.invalid/releases/plugin/manifest.json",
        "manifestDigest": verification["manifest_digest"],
        "manifestAssetDigest": _digest("release-manifest-asset"),
        "digest": record["archive_digest"],
        "targetId": "linux-u24-plugin",
        "indexIdentity": {"source": "signed-release-index"},
        "publisherIdentity": {"repository": "Example/Plugins"},
        "attestationRef": {"bundleDigest": _digest("attestation")},
        "packageArtifactDigest": record["artifact_digest"],
        "archiveDigest": record["archive_digest"],
        "descriptorDigest": verification["descriptor_digest"],
        "dependencyLockDigest": verification["dependency_lock_digest"],
    }


def _maintenance(
    component_id: str = "cyrene-tools-plugin-a",
    artifact_digest: str | None = None,
    *,
    target_kind: str = "PACKAGE_ONLY",
    transaction_id: str = "cyrene-workload-package-only-plan-001",
) -> dict[str, Any]:
    return {
        "transaction_id": transaction_id,
        "maintenance_token": "private-maintenance-token",
        "target_kind": target_kind,
        "plan_id": "plan-" + "a" * 32,
        "plan_digest": _digest("parent-plan"),
        "component_artifact_digests": {component_id: artifact_digest or _digest("artifact-1.2.3")},
        "expected_gate_generation": 9,
        "expected_catalog_generation": 5,
    }


def test_source_update_registers_product_with_empty_initial_catalog() -> None:
    source_id = "cyrene-catalyst"
    update = runtime.build_workload_source_update(
        source_policy=_source_policy(),
        selected_rows=[],
        installation_records={},
        activity_catalog={"schema_version": 1, "generation": 0, "sources": []},
        source_principals={
            source_id: _principal(
                source_id, uid=12001, gid=12002, token_path=Path("/tokens/catalyst.token")
            )
        },
    )

    assert update.changed is True
    assert update.expected_generation == 1
    assert update.binding_scopes == {source_id: []}
    assert update.runtime_bindings == {source_id: []}
    assert update.source_arguments == ("--source", f"{source_id}=12001:12002")
    assert update.previous_policy_digest is None


def test_source_update_retains_existing_installation_and_other_owner_scopes() -> None:
    row = _package_row()
    prior = _installation_record(version="1.1.0")
    current = _installation_record(version="1.2.3")
    old_binding_id = "plugin-a-catalyst"
    other_binding_id = "other-tools-binding"
    product_id = "cyrene-catalyst"
    other_id = "external-operator"
    product_scope = _broker_scope(old_binding_id, row["packageId"], [prior["installation_id"]])
    other_scope = _broker_scope(other_binding_id, "org.example.other", ["installation-" + "b" * 32])
    catalog = _activity_catalog(
        [
            {
                "source_id": product_id,
                "uid": 12001,
                "gid": 12002,
                "source_token_sha256": _raw_digest("product-token"),
                "binding_scopes": [product_scope],
            },
            {
                "source_id": other_id,
                "uid": 12003,
                "gid": 12004,
                "source_token_sha256": _raw_digest("other-token"),
                "binding_scopes": [other_scope],
            },
        ]
    )
    policy = _runtime_policy(
        [
            {
                "source_id": product_id,
                "uid": 12001,
                "gid": 12002,
                "source_token_sha256": _raw_digest("product-token"),
                "bindings": [
                    _runtime_binding(old_binding_id, row["packageId"], [prior["installation_id"]])
                ],
            },
            {
                "source_id": other_id,
                "uid": 12003,
                "gid": 12004,
                "source_token_sha256": _raw_digest("other-token"),
                "bindings": [
                    _runtime_binding(
                        other_binding_id, "org.example.other", ["installation-" + "b" * 32]
                    )
                ],
            },
        ]
    )
    principals = {
        product_id: _principal(
            product_id, uid=12001, gid=12002, token_path=Path("/tokens/product.token")
        ),
        other_id: _principal(
            other_id, uid=12003, gid=12004, token_path=Path("/tokens/other.token")
        ),
    }

    update = runtime.build_workload_source_update(
        source_policy=row["sourcePolicy"],
        selected_rows=[row],
        installation_records={row["componentId"]: current},
        activity_catalog=catalog,
        source_principals=principals,
        runtime_policy=policy,
    )

    expected_installations = sorted([prior["installation_id"], current["installation_id"]])
    product_binding = update.binding_scopes[product_id][0]
    assert product_binding["installation_ids"] == expected_installations
    assert product_binding["operations"] == list(runtime.BROKER_OPERATIONS)
    assert update.runtime_bindings[product_id][0]["installation_ids"] == expected_installations
    assert update.binding_scopes[other_id] == [other_scope]
    assert update.runtime_bindings[other_id] == policy["sources"][1]["bindings"]
    assert update.expected_generation == 6
    assert {"--source", f"{other_id}=12003:12004"}.issubset(set(update.source_arguments))


def test_source_update_rejects_binding_owned_by_another_source() -> None:
    row = _package_row(binding_id="shared-binding")
    record = _installation_record()
    first_source = {
        "source_id": "cyrene-catalyst",
        "uid": 12001,
        "gid": 12002,
        "source_token_sha256": _raw_digest("product-token"),
        "binding_scopes": [],
    }
    second_source = {
        "source_id": "external-owner",
        "uid": 12003,
        "gid": 12004,
        "source_token_sha256": _raw_digest("external-token"),
        "binding_scopes": [
            _broker_scope("shared-binding", row["packageId"], [record["installation_id"]])
        ],
    }
    policy = _runtime_policy(
        [
            {
                "source_id": "cyrene-catalyst",
                "uid": 12001,
                "gid": 12002,
                "source_token_sha256": _raw_digest("product-token"),
                "bindings": [],
            },
            {
                "source_id": "external-owner",
                "uid": 12003,
                "gid": 12004,
                "source_token_sha256": _raw_digest("external-token"),
                "bindings": [
                    _runtime_binding(
                        "shared-binding", row["packageId"], [record["installation_id"]]
                    )
                ],
            },
        ]
    )
    with pytest.raises(runtime.WorkloadPackageRuntimeError, match="already owned"):
        runtime.build_workload_source_update(
            source_policy=row["sourcePolicy"],
            selected_rows=[row],
            installation_records={row["componentId"]: record},
            activity_catalog=_activity_catalog([first_source, second_source]),
            source_principals={
                "cyrene-catalyst": _principal(
                    "cyrene-catalyst", uid=12001, gid=12002, token_path=Path("/tokens/a")
                ),
                "external-owner": _principal(
                    "external-owner", uid=12003, gid=12004, token_path=Path("/tokens/b")
                ),
            },
            runtime_policy=policy,
        )


def test_package_hold_binds_component_to_aggregate_digest_and_rejects_core_hold() -> None:
    artifact_digest = _digest("package-aggregate")
    stage = {"stagedIdentity": {"planId": "plan-" + "a" * 32, "planDigest": _digest("parent-plan")}}
    hold = _maintenance(artifact_digest=artifact_digest)
    runtime._validate_phase_identity(
        stage, "cyrene-install-request-1", hold, "cyrene-tools-plugin-a", artifact_digest
    )

    with pytest.raises(runtime.WorkloadPackageRuntimeError, match="PACKAGE_ONLY"):
        runtime._validate_phase_identity(
            stage,
            "cyrene-install-request-2",
            {**hold, "target_kind": "CORE_RUNTIME"},
            "cyrene-tools-plugin-a",
            artifact_digest,
        )
    with pytest.raises(runtime.WorkloadPackageRuntimeError, match="digest map"):
        runtime._validate_phase_identity(
            stage,
            "cyrene-install-request-3",
            hold,
            "cyrene-tools-plugin-a",
            _digest("archive-bytes"),
        )


def test_source_policy_hold_accepts_core_registration_target() -> None:
    hold = _maintenance(target_kind="CORE_RUNTIME", component_id="cyrene-catalyst")
    assert runtime._validate_policy_update_hold(hold)["target_kind"] == "CORE_RUNTIME"


def test_apply_source_update_commits_gen_zero_catalog_and_matching_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = "cyrene-catalyst"
    source_uid, source_gid = 12001, 12002
    token_dir = tmp_path / "tokens"
    catalog_path = tmp_path / "activity-sources.json"
    policy_path = tmp_path / "runtime-package-sources.json"
    staging_root = tmp_path / "bootstrap-stage"
    source_token = "core-source-token-private"
    source_digest = hashlib.sha256(source_token.encode()).hexdigest()
    update = runtime.build_workload_source_update(
        source_policy=_source_policy(),
        selected_rows=[],
        installation_records={},
        activity_catalog={"schema_version": 1, "generation": 0, "sources": []},
        source_principals={
            source_id: _principal(
                source_id,
                uid=source_uid,
                gid=source_gid,
                token_path=token_dir / f"{source_id}.token",
            )
        },
    )
    monkeypatch.setattr(runtime, "_require_root", lambda: None)
    monkeypatch.setattr(runtime, "_maintenance_group_id", lambda: os.getgid())
    monkeypatch.setattr(runtime, "_runtime_group_id", lambda: os.getgid())
    monkeypatch.setattr(runtime, "_validate_policy_parent", lambda _path, _owner: None)
    monkeypatch.setattr(
        runtime,
        "_ensure_private_dir",
        lambda path, parent=None: Path(path).mkdir(parents=True, mode=0o700, exist_ok=True),
    )
    monkeypatch.setattr(
        runtime,
        "_write_private_json",
        lambda path, value: Path(path).write_bytes(_canonical(value)),
    )
    bootstrap = runtime._bootstrap_module()
    monkeypatch.setattr(bootstrap, "PACKAGE_BOOTSTRAP_STAGE_ROOT", staging_root)

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        arguments = dict(pairwise(argv))
        catalog_file = Path(argv[argv.index("--catalog") + 1])
        token_directory = Path(argv[argv.index("--token-dir") + 1])
        scopes_file = Path(argv[argv.index("--binding-scopes-json") + 1])
        scope_map = json.loads(scopes_file.read_text(encoding="utf-8"))
        assert arguments["--catalog"] == str(catalog_file)
        assert argv[argv.index("--source") + 1] == f"{source_id}={source_uid}:{source_gid}"
        token_directory.mkdir(mode=0o700, exist_ok=True)
        token_path = token_directory / f"{source_id}.token"
        token_path.write_text(source_token + "\n", encoding="utf-8")
        token_path.chmod(0o400)
        source_row = {
            "source_id": source_id,
            "uid": source_uid,
            "gid": source_gid,
            "source_token_sha256": source_digest,
            "binding_scopes": scope_map[source_id],
        }
        catalog_file.write_text(
            json.dumps({"schema_version": 1, "generation": 1, "sources": [source_row]}),
            encoding="utf-8",
        )
        catalog_file.chmod(0o600)
        receipt = {
            "schema_version": 1,
            "generation": 1,
            "sources": [
                {
                    "source_id": source_id,
                    "token_file": str(token_path),
                    "binding_scope_count": 0,
                }
            ],
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(receipt), "")

    result = runtime.apply_workload_source_update(
        update,
        maintenance=_maintenance(target_kind="CORE_RUNTIME", component_id="cyrene-catalyst"),
        request_id="cyrene-register-product-source",
        expected_policy_digest=None,
        activity_catalog_path=catalog_path,
        policy_path=policy_path,
        token_directory=token_dir,
        command=Path("/usr/bin/cyrene"),
        runner=runner,
    )

    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    assert result["catalogGeneration"] == result["policyGeneration"] == 1
    assert result["sourceIdentities"] == [
        {"sourceId": source_id, "uid": source_uid, "gid": source_gid}
    ]
    assert policy["generation"] == 1
    assert policy["sources"][0]["source_token_sha256"] == source_digest
    assert source_token not in json.dumps(result)
    assert "private-maintenance-token" not in json.dumps(result)


def test_source_update_requires_held_aggregate_and_product_identity_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _package_row(standalone=True, binding_id="plugin-a-standalone")
    record = _installation_record()
    source_id = "cyrene-plugin-standalone-operator"
    update = runtime.build_workload_source_update(
        source_policy=row["sourcePolicy"],
        selected_rows=[row],
        installation_records={row["componentId"]: record},
        activity_catalog={"schema_version": 1, "generation": 0, "sources": []},
        source_principals={
            source_id: _principal(
                source_id,
                uid=12001,
                gid=12002,
                token_path=tmp_path / f"{source_id}.token",
            )
        },
    )
    monkeypatch.setattr(runtime, "_require_root", lambda: None)
    monkeypatch.setattr(runtime, "_maintenance_group_id", lambda: os.getgid())
    monkeypatch.setattr(runtime, "_runtime_group_id", lambda: os.getgid())
    monkeypatch.setattr(
        runtime,
        "_run_json_command",
        lambda *_args, **_kwargs: pytest.fail("a package outside the held map must not mutate"),
    )

    with pytest.raises(runtime.WorkloadPackageRuntimeError, match="outside the held plan"):
        runtime.apply_workload_source_update(
            update,
            maintenance=_maintenance(row["componentId"], _digest("wrong-artifact")),
            request_id="cyrene-source-update-wrong-digest",
            expected_policy_digest=None,
            activity_catalog_path=tmp_path / "activity-catalog.json",
            policy_path=tmp_path / "runtime-package-sources.json",
            token_directory=tmp_path / "tokens",
        )

    product_id = "cyrene-catalyst"
    product_update = runtime.build_workload_source_update(
        source_policy=_source_policy(),
        selected_rows=[],
        installation_records={},
        activity_catalog={"schema_version": 1, "generation": 0, "sources": []},
        source_principals={
            product_id: _principal(
                product_id,
                uid=12003,
                gid=12004,
                token_path=tmp_path / f"{product_id}.token",
            )
        },
    )
    with pytest.raises(runtime.WorkloadPackageRuntimeError, match="source identity is outside"):
        runtime.apply_workload_source_update(
            product_update,
            maintenance=_maintenance("another-component"),
            request_id="cyrene-source-update-wrong-product",
            expected_policy_digest=None,
            activity_catalog_path=tmp_path / "activity-catalog.json",
            policy_path=tmp_path / "runtime-package-sources.json",
            token_directory=tmp_path / "tokens",
        )


def test_source_update_rejects_policy_race_before_broker_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = "cyrene-catalyst"
    update = runtime.build_workload_source_update(
        source_policy=_source_policy(),
        selected_rows=[],
        installation_records={},
        activity_catalog={"schema_version": 1, "generation": 0, "sources": []},
        source_principals={
            source_id: _principal(
                source_id,
                uid=12001,
                gid=12002,
                token_path=tmp_path / f"{source_id}.token",
            )
        },
    )
    monkeypatch.setattr(runtime, "_require_root", lambda: None)
    monkeypatch.setattr(runtime, "_maintenance_group_id", lambda: os.getgid())
    monkeypatch.setattr(runtime, "_runtime_group_id", lambda: os.getgid())
    monkeypatch.setattr(runtime, "_validate_policy_parent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime, "_read_policy_bytes_optional", lambda *_args, **_kwargs: b"raced-policy"
    )
    monkeypatch.setattr(
        runtime,
        "_run_json_command",
        lambda *_args, **_kwargs: pytest.fail(
            "stale policy must be detected before broker mutation"
        ),
    )

    with pytest.raises(
        runtime.WorkloadPackageRuntimeError, match="changed after source projection"
    ):
        runtime.apply_workload_source_update(
            update,
            maintenance=_maintenance("cyrene-catalyst"),
            request_id="cyrene-source-update-raced-policy",
            expected_policy_digest=None,
            activity_catalog_path=tmp_path / "activity-catalog.json",
            policy_path=tmp_path / "runtime-package-sources.json",
            token_directory=tmp_path / "tokens",
        )


def test_reconcile_source_update_finishes_broker_commit_before_policy_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _package_row(standalone=True, binding_id="plugin-a-standalone")
    record = _installation_record()
    source_id = "cyrene-plugin-standalone-operator"
    source_uid, source_gid = 12001, 12002
    token_directory = tmp_path / "tokens"
    source_digest = _raw_digest("standalone-token")
    old_runtime_source = {
        "source_id": source_id,
        "uid": source_uid,
        "gid": source_gid,
        "source_token_sha256": source_digest,
        "bindings": [],
    }
    old_policy = _runtime_policy([old_runtime_source], generation=5)
    old_policy_bytes = _canonical(old_policy)
    policy_path = tmp_path / "runtime-package-sources.json"
    policy_path.write_bytes(old_policy_bytes)
    policy_path.chmod(0o440)
    catalog_path = tmp_path / "activity-sources.json"
    update = runtime.build_workload_source_update(
        source_policy=row["sourcePolicy"],
        selected_rows=[row],
        installation_records={row["componentId"]: record},
        activity_catalog=_activity_catalog(
            [
                {
                    "source_id": source_id,
                    "uid": source_uid,
                    "gid": source_gid,
                    "source_token_sha256": source_digest,
                    "binding_scopes": [],
                }
            ],
            generation=5,
        ),
        source_principals={
            source_id: _principal(
                source_id,
                uid=source_uid,
                gid=source_gid,
                token_path=token_directory / f"{source_id}.token",
            )
        },
        runtime_policy=old_policy,
    )
    source_scope = {
        "source_id": source_id,
        "uid": source_uid,
        "gid": source_gid,
        "source_token_sha256": source_digest,
        "binding_scopes": update.binding_scopes[source_id],
    }
    catalog_path.write_bytes(
        _canonical(_activity_catalog([source_scope], generation=update.expected_generation))
    )
    catalog_path.chmod(0o600)

    monkeypatch.setattr(runtime, "_require_root", lambda: None)
    monkeypatch.setattr(runtime, "_effective_uid", os.getuid)
    monkeypatch.setattr(runtime, "_runtime_group_id", os.getgid)
    monkeypatch.setattr(runtime, "_validate_policy_parent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_verify_token_file", lambda *_args, **_kwargs: None)

    result = runtime.reconcile_workload_source_update(
        update,
        maintenance=_maintenance(row["componentId"], record["artifact_digest"]),
        activity_catalog_path=catalog_path,
        policy_path=policy_path,
        token_directory=token_directory,
    )

    assert result is not None
    assert result["catalogGeneration"] == result["policyGeneration"] == update.expected_generation
    assert json.loads(policy_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "generation": update.expected_generation,
        "sources": [
            {
                **old_runtime_source,
                "bindings": update.runtime_bindings[source_id],
            }
        ],
    }
    assert result["bindings"] == [
        {
            "componentId": row["componentId"],
            "sourceId": source_id,
            "bindingId": row["bindingId"],
        }
    ]


def test_reconcile_source_update_refuses_unrelated_runtime_policy_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _package_row(standalone=True, binding_id="plugin-a-standalone")
    record = _installation_record()
    source_id = "cyrene-plugin-standalone-operator"
    source_uid, source_gid = 12001, 12002
    token_directory = tmp_path / "tokens"
    source_digest = _raw_digest("standalone-token")
    old_runtime_source = {
        "source_id": source_id,
        "uid": source_uid,
        "gid": source_gid,
        "source_token_sha256": source_digest,
        "bindings": [],
    }
    old_policy = _runtime_policy([old_runtime_source], generation=5)
    policy_path = tmp_path / "runtime-package-sources.json"
    policy_path.write_bytes(_canonical(old_policy))
    policy_path.chmod(0o440)
    catalog_path = tmp_path / "activity-sources.json"
    update = runtime.build_workload_source_update(
        source_policy=row["sourcePolicy"],
        selected_rows=[row],
        installation_records={row["componentId"]: record},
        activity_catalog=_activity_catalog(
            [
                {
                    "source_id": source_id,
                    "uid": source_uid,
                    "gid": source_gid,
                    "source_token_sha256": source_digest,
                    "binding_scopes": [],
                }
            ],
            generation=5,
        ),
        source_principals={
            source_id: _principal(
                source_id,
                uid=source_uid,
                gid=source_gid,
                token_path=token_directory / f"{source_id}.token",
            )
        },
        runtime_policy=old_policy,
    )
    source_scope = {
        "source_id": source_id,
        "uid": source_uid,
        "gid": source_gid,
        "source_token_sha256": source_digest,
        "binding_scopes": update.binding_scopes[source_id],
    }
    catalog_path.write_bytes(
        _canonical(_activity_catalog([source_scope], generation=update.expected_generation))
    )
    catalog_path.chmod(0o600)
    raced_policy = _runtime_policy(
        [
            {
                **old_runtime_source,
                "bindings": [
                    _runtime_binding(
                        "plugin-a-standalone",
                        "org.example.plugin-a",
                        [_installation_record(version="1.2.4")["installation_id"]],
                    )
                ],
            }
        ],
        generation=5,
    )
    policy_path.chmod(0o600)
    policy_path.write_bytes(_canonical(raced_policy))
    policy_path.chmod(0o440)

    monkeypatch.setattr(runtime, "_require_root", lambda: None)
    monkeypatch.setattr(runtime, "_effective_uid", os.getuid)
    monkeypatch.setattr(runtime, "_runtime_group_id", os.getgid)
    monkeypatch.setattr(runtime, "_validate_policy_parent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_verify_token_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "write_runtime_source_policy_cas",
        lambda *_args, **_kwargs: pytest.fail("an unrelated policy must never be overwritten"),
    )

    with pytest.raises(
        runtime.WorkloadPackageRuntimeError, match="outside the pending source update"
    ):
        runtime.reconcile_workload_source_update(
            update,
            maintenance=_maintenance(row["componentId"], record["artifact_digest"]),
            activity_catalog_path=catalog_path,
            policy_path=policy_path,
            token_directory=token_directory,
        )


def test_policy_cas_serializes_competing_writers_and_rejects_stale_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = {
        "source_id": "cyrene-catalyst",
        "uid": 12001,
        "gid": 12002,
        "source_token_sha256": _raw_digest("token"),
        "bindings": [],
    }
    first = _runtime_policy([source], generation=1)
    path = tmp_path / "runtime-package-sources.json"
    monkeypatch.setattr(runtime, "_require_root", lambda: None)
    monkeypatch.setattr(runtime, "_runtime_group_id", lambda: os.getgid())
    monkeypatch.setattr(runtime, "_validate_policy_parent", lambda _path, _owner: None)
    digest = runtime.write_runtime_source_policy_cas(
        first, expected_prior_digest=None, policy_path=path
    )
    barrier = threading.Barrier(2)
    policies = []
    for binding_id in ("candidate-binding-a", "candidate-binding-b"):
        binding = _runtime_binding(
            binding_id, "org.example.plugin", ["installation-" + binding_id[-1] * 32]
        )
        policies.append(_runtime_policy([{**source, "bindings": [binding]}], generation=2))

    def update(policy: dict[str, Any]) -> str:
        barrier.wait()
        return runtime.write_runtime_source_policy_cas(
            policy,
            expected_prior_digest=digest,
            policy_path=path,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update, policy) for policy in policies]
    outcomes: list[str] = []
    for future in futures:
        try:
            future.result()
        except runtime.WorkloadPackageRuntimeError:
            outcomes.append("stale")
        else:
            outcomes.append("committed")

    assert sorted(outcomes) == ["committed", "stale"]
    assert json.loads(path.read_text(encoding="utf-8")) in policies


def test_policy_cas_restores_previous_bytes_after_failed_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = {
        "source_id": "cyrene-catalyst",
        "uid": 12001,
        "gid": 12002,
        "source_token_sha256": _raw_digest("token"),
        "bindings": [],
    }
    original = _runtime_policy([source], generation=1)
    replacement = _runtime_policy([source], generation=2)
    path = tmp_path / "runtime-package-sources.json"
    monkeypatch.setattr(runtime, "_require_root", lambda: None)
    monkeypatch.setattr(runtime, "_runtime_group_id", lambda: os.getgid())
    monkeypatch.setattr(runtime, "_validate_policy_parent", lambda _path, _owner: None)
    original_digest = runtime.write_runtime_source_policy_cas(
        original, expected_prior_digest=None, policy_path=path
    )
    original_bytes = path.read_bytes()
    original_reader = runtime._read_policy_bytes_optional
    reads = 0

    def fail_one_readback(*args: Any, **kwargs: Any) -> bytes | None:
        nonlocal reads
        reads += 1
        if reads == 3:
            return b"injected readback failure"
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(runtime, "_read_policy_bytes_optional", fail_one_readback)
    with pytest.raises(runtime.WorkloadPackageRuntimeError, match="readback failed"):
        runtime.write_runtime_source_policy_cas(
            replacement,
            expected_prior_digest=original_digest,
            policy_path=path,
        )

    assert path.read_bytes() == original_bytes
    assert json.loads(original_bytes) == original


def test_inventory_returns_full_record_and_all_owner_binding_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _installation_record()
    row = _package_row(standalone=True, binding_id="plugin-a-standalone")
    installation_id = record["installation_id"]
    product_id = "cyrene-catalyst"
    standalone_id = "cyrene-plugin-standalone-operator"
    product_binding_id = "catalyst-plugin-a"
    product_scope = _broker_scope(product_binding_id, row["packageId"], [installation_id])
    plugin_scope = _broker_scope(row["bindingId"], row["packageId"], [installation_id])
    catalog = _activity_catalog(
        [
            {
                "source_id": product_id,
                "uid": 12001,
                "gid": 12002,
                "source_token_sha256": _raw_digest("product-token"),
                "binding_scopes": [product_scope],
            },
            {
                "source_id": standalone_id,
                "uid": 12003,
                "gid": 12004,
                "source_token_sha256": _raw_digest("standalone-token"),
                "binding_scopes": [plugin_scope],
            },
        ]
    )
    policy = _runtime_policy(
        [
            {
                "source_id": product_id,
                "uid": 12001,
                "gid": 12002,
                "source_token_sha256": _raw_digest("product-token"),
                "bindings": [
                    _runtime_binding(product_binding_id, row["packageId"], [installation_id])
                ],
            },
            {
                "source_id": standalone_id,
                "uid": 12003,
                "gid": 12004,
                "source_token_sha256": _raw_digest("standalone-token"),
                "bindings": [
                    _runtime_binding(row["bindingId"], row["packageId"], [installation_id])
                ],
            },
        ]
    )
    principals = {
        product_id: _principal(
            product_id, uid=12001, gid=12002, token_path=tmp_path / "product.token"
        ),
        standalone_id: _principal(
            standalone_id,
            uid=12003,
            gid=12004,
            token_path=tmp_path / "standalone.token",
        ),
    }
    monkeypatch.setattr(runtime, "_require_root", lambda: None)
    monkeypatch.setattr(runtime, "_read_activity_catalog", lambda _path: catalog)
    monkeypatch.setattr(runtime, "read_runtime_source_policy_generic", lambda _path: policy)
    monkeypatch.setattr(runtime, "_verify_token_file", lambda *_args: None)

    def sdk_read(**request: Any) -> dict[str, Any]:
        if request["operation"] == "get_installation":
            return {"installation": record}
        active = request["source_id"] == product_id
        return {
            "status": {
                "binding_id": request["binding_id"],
                "installation_id": installation_id,
                "generation": 8,
                "state": "RUNNING" if active else "STOPPED",
                "failure_code": None,
            }
        }

    monkeypatch.setattr(runtime, "run_package_binding_operation", sdk_read)
    inventory = runtime.read_workload_package_inventory(
        selected_rows=[row],
        source_principals=principals,
        sdk_python=Path("/opt/cyrene/operator/venv/bin/python"),
        activity_catalog_path=tmp_path / "catalog.json",
        policy_path=tmp_path / "policy.json",
        token_directory=tmp_path,
        installation_hints={row["componentId"]: _hint(record)},
    )

    assert inventory["installationRecords"][row["componentId"]] == record
    assert inventory["components"][row["componentId"]]["installationId"] == installation_id
    assert len(inventory["sourceBindings"]) == 2
    active_owner = next(
        item for item in inventory["sourceBindings"] if item["sourceId"] == product_id
    )
    assert active_owner["activeInstallationId"] == installation_id


def test_sdk_callback_bridge_requires_journal_ack_before_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = "cyrene-catalyst"
    binding_id = "plugin-a-catalyst"
    package_id = "org.example.plugin-a"
    installation_id = _installation_record()["installation_id"]
    uid, gid = 12001, 12002
    token_path = tmp_path / f"{source_id}.token"
    token_path.write_text("source-token\n", encoding="utf-8")
    status = {
        "binding_id": binding_id,
        "installation_id": installation_id,
        "generation": 4,
        "state": "RUNNING",
        "failure_code": None,
        "failure_message": None,
        "connection_ref": "grpc://127.0.0.1:42819",
    }
    receipt = {
        "request_id": "cyrene-plugin-activate-1",
        "source_id": source_id,
        "protocol_version": runtime.BINDING_OPERATION_PROTOCOL,
        "catalog_generation": 5,
        "scope": {
            "binding_id": binding_id,
            "package_id": package_id,
            "installation_id": installation_id,
            "operation": "activate",
        },
        "operation_token": "private-operation-receipt-token",
        "gate_generation": 9,
        "already_in_flight": False,
        "already_completed": False,
    }
    catalog = _activity_catalog(
        [
            {
                "source_id": source_id,
                "uid": uid,
                "gid": gid,
                "source_token_sha256": _raw_digest("source-token"),
                "binding_scopes": [_broker_scope(binding_id, package_id, [installation_id])],
            }
        ]
    )
    policy = _runtime_policy(
        [
            {
                "source_id": source_id,
                "uid": uid,
                "gid": gid,
                "source_token_sha256": _raw_digest("source-token"),
                "bindings": [_runtime_binding(binding_id, package_id, [installation_id])],
            }
        ]
    )
    principals = {source_id: _principal(source_id, uid=uid, gid=gid, token_path=token_path)}
    monkeypatch.setattr(runtime, "_require_root", lambda: None)
    monkeypatch.setattr(runtime, "_validate_owner_call", lambda **_kwargs: None)
    monkeypatch.setattr(runtime, "_validate_sdk_python", lambda _path: None)
    monkeypatch.setattr(runtime, "_read_token", lambda *_args: "source-token")
    create_memfd = runtime._create_token_memfd
    monkeypatch.setattr(
        runtime, "_create_token_memfd", lambda token, **_kwargs: create_memfd(token)
    )
    monkeypatch.setattr(runtime, "_verify_process_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("pwd.getpwuid", lambda _uid: SimpleNamespace(pw_uid=uid))
    monkeypatch.setattr("grp.getgrgid", lambda _gid: SimpleNamespace(gr_gid=gid))
    bridge_mode = {"value": "outcome"}

    def process_factory(original_argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        driver_request = json.loads(original_argv[-1])
        fake_driver = r"""import json, os, socket, sys
channel = socket.socket(fileno=int(sys.argv[1]))
request = json.loads(sys.argv[2])
def send(value):
    channel.sendall(json.dumps(value, separators=(",", ":")).encode() + b"\n")
    data = bytearray()
    while not data.endswith(b"\n"):
        block = channel.recv(1)
        if not block:
            raise SystemExit(7)
        data.extend(block)
    if json.loads(data).get("ok") is not True:
        raise SystemExit(8)
send({"kind":"identity","uid":request["source_uid"],"euid":request["source_uid"],"gid":request["source_gid"],"egid":request["source_gid"],"groups":[]})
send({"kind":"callback","payload":{"event":"intent","requestId":request["request_id"],"scope":{"binding_id":request["binding_id"],"package_id":request["package_id"],"installation_id":request["installation_ids"][0],"operation":"activate"}}})
if sys.argv[6] == "reconcile":
    send({"kind":"callback","payload":{"event":"reconcile","status":None,"installation":json.loads(sys.argv[5]),"receipt":json.loads(sys.argv[4])}})
    summary = {"bindingId":request["binding_id"],"installationId":request["installation_ids"][0],"generation":None,"state":"STOPPED","failureCode":None,"reconciled":True}
else:
    send({"kind":"callback","payload":{"event":"outcome","status":json.loads(sys.argv[3]),"receipt":json.loads(sys.argv[4])}})
    summary = {"bindingId":request["binding_id"],"installationId":request["installation_ids"][0],"generation":4,"state":"RUNNING","failureCode":None,"reconciled":False}
send({"kind":"summary","value":summary})
print(json.dumps({"ok":True,"kind":"complete","value":{"requestId":request["request_id"]}},separators=(",", ":")))
"""
        fake_kwargs = dict(kwargs)
        fake_kwargs.pop("preexec_fn", None)
        fake_kwargs.pop("stdin", None)
        fake_kwargs["pass_fds"] = kwargs["pass_fds"]
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                fake_driver,
                original_argv[4],
                json.dumps(driver_request),
                json.dumps(status),
                json.dumps(receipt),
                json.dumps(_installation_record()),
                bridge_mode["value"],
            ],
            stdin=subprocess.DEVNULL,
            **fake_kwargs,
        )

    intents: list[tuple[str, dict[str, Any]]] = []
    outcomes: list[tuple[dict[str, Any] | None, dict[str, Any]]] = []
    request_id = "cyrene-plugin-activate-1"

    def persist_intent(value: str, scope: dict[str, Any]) -> None:
        intents.append((value, scope))

    def persist_outcome(value: dict[str, Any] | None, proof: dict[str, Any]) -> None:
        outcomes.append((value, proof))

    result = runtime.run_package_binding_operation(
        operation="activate",
        source_id=source_id,
        uid=uid,
        gid=gid,
        token_path=token_path,
        binding_id=binding_id,
        package_id=package_id,
        installation_ids=[installation_id],
        catalog_generation=5,
        request_id=request_id,
        sdk_python=Path("/opt/cyrene/operator/venv/bin/python"),
        activity_catalog=catalog,
        runtime_policy=policy,
        source_principals=principals,
        persist_intent=persist_intent,
        persist_outcome=persist_outcome,
        persist_reconcile=lambda *_args: None,
        process_factory=process_factory,
    )

    assert intents == [
        (
            request_id,
            {
                "binding_id": binding_id,
                "package_id": package_id,
                "installation_id": installation_id,
                "operation": "activate",
            },
        )
    ]
    assert len(outcomes) == 1
    assert outcomes[0][0]["connection_ref"] == status["connection_ref"]
    assert outcomes[0][1]["request_id"] == receipt["request_id"]
    assert "operation_token" not in outcomes[0][1]
    assert "failure_message" not in outcomes[0][0]
    assert result["state"] == "RUNNING"
    assert receipt["operation_token"] not in json.dumps(result)
    assert receipt["operation_token"] not in json.dumps(outcomes)

    reconciliations: list[tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]] = []
    bridge_mode["value"] = "reconcile"
    recovered = runtime.run_package_binding_operation(
        operation="activate",
        source_id=source_id,
        uid=uid,
        gid=gid,
        token_path=token_path,
        binding_id=binding_id,
        package_id=package_id,
        installation_ids=[installation_id],
        catalog_generation=5,
        request_id=request_id,
        sdk_python=Path("/opt/cyrene/operator/venv/bin/python"),
        activity_catalog=catalog,
        runtime_policy=policy,
        source_principals=principals,
        persist_intent=persist_intent,
        persist_outcome=lambda *_args: pytest.fail(
            "recovery must not persist a second mutation outcome"
        ),
        persist_reconcile=lambda *args: reconciliations.append(args),
        process_factory=process_factory,
    )
    assert recovered["reconciled"] is True
    assert recovered["state"] == "STOPPED"
    assert len(reconciliations) == 1
    assert reconciliations[0][0] is None
    assert reconciliations[0][1]["installation_id"] == installation_id
    assert "operation_token" not in reconciliations[0][2]
    bridge_mode["value"] = "outcome"

    def fail_outcome(_status: dict[str, Any] | None, _proof: dict[str, Any]) -> None:
        raise OSError("durable journal unavailable")

    with pytest.raises(runtime.WorkloadPackageRuntimeError, match="journal callback"):
        runtime.run_package_binding_operation(
            operation="activate",
            source_id=source_id,
            uid=uid,
            gid=gid,
            token_path=token_path,
            binding_id=binding_id,
            package_id=package_id,
            installation_ids=[installation_id],
            catalog_generation=5,
            request_id=request_id,
            sdk_python=Path("/opt/cyrene/operator/venv/bin/python"),
            activity_catalog=catalog,
            runtime_policy=policy,
            source_principals=principals,
            persist_intent=persist_intent,
            persist_outcome=fail_outcome,
            persist_reconcile=lambda *_args: None,
            process_factory=process_factory,
        )


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root-owned interpreter fixture")
def test_sdk_python_accepts_only_a_root_controlled_final_venv_symlink() -> None:
    with tempfile.TemporaryDirectory(prefix="cyrene-sdk-test-", dir="/root") as root:
        base = Path(root)
        executable = base / "python3.12"
        executable.write_bytes(b"verified interpreter fixture")
        executable.chmod(0o755)
        venv_bin = base / "releases" / "sdk" / "venv" / "bin"
        venv_bin.mkdir(parents=True, mode=0o755)
        (venv_bin / "python").symlink_to(executable)

        runtime._validate_sdk_python(venv_bin / "python")

        alias = base / "venv-alias"
        alias.symlink_to(venv_bin.parent.parent)
        with pytest.raises(runtime.WorkloadPackageRuntimeError, match="unsafe symlink"):
            runtime._validate_sdk_python(alias / "bin" / "python")


def test_source_token_memfd_is_sealed_before_sdk_handoff() -> None:
    import fcntl

    descriptor = runtime._create_token_memfd("private-test-token")
    try:
        info = os.fstat(descriptor)
        assert info.st_uid == os.geteuid()
        assert info.st_gid == os.getegid()
        assert stat.S_IMODE(info.st_mode) == 0o400
        assert os.read(descriptor, 64) == b"private-test-token"
        seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
        required = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
        assert seals & required == required
        with pytest.raises(OSError):
            os.write(descriptor, b"x")
    finally:
        os.close(descriptor)


def test_uninstall_uses_exact_udS_installation_record_and_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _installation_record()
    component_id = "cyrene-tools-plugin-a"
    hold = _maintenance(component_id, record["artifact_digest"])
    bootstrap = runtime._bootstrap_module()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(runtime, "_require_root", lambda: None)
    monkeypatch.setattr(runtime, "_bootstrap_module", lambda: bootstrap)

    def stage(**kwargs: Any) -> tuple[Path, Path, dict[str, Any]]:
        captured.update(kwargs)
        request = {"request_id": kwargs["request_id"], "installation": kwargs["installation"]}
        return tmp_path, tmp_path / "request.json", request

    def run(request: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        captured["request"] = request
        return {"installation": request["installation"], "already_absent": False}

    monkeypatch.setattr(bootstrap, "stage_workload_offline_uninstall_request", stage)
    monkeypatch.setattr(bootstrap, "run_workload_offline_uninstall", run)
    result = runtime.uninstall_workload_package(
        record,
        component_id=component_id,
        request_id="cyrene-uninstall-request-1",
        maintenance=hold,
        staging_root=tmp_path,
        command=Path("/usr/bin/cyrene"),
    )

    assert captured["installation"] == {
        "component_id": component_id,
        "installation_id": record["installation_id"],
        "package_id": record["package_id"],
        "package_version": record["package_version"],
        "artifact_digest": record["artifact_digest"],
        "archive_digest": record["archive_digest"],
        "descriptor_digest": record["verification"]["descriptor_digest"],
        "manifest_digest": record["verification"]["manifest_digest"],
        "dependency_lock_digest": record["verification"]["dependency_lock_digest"],
    }
    assert result["installation"] == captured["installation"]
    assert result["already_absent"] is False

    def referenced(**_kwargs: Any) -> Any:
        raise runtime._bootstrap_module().PackageRuntimeBootstrapError(
            "installation is still referenced by another owner binding"
        )

    monkeypatch.setattr(bootstrap, "run_workload_offline_uninstall", referenced)
    with pytest.raises(
        runtime.WorkloadPackageRuntimeError, match="offline package uninstall did not complete"
    ):
        runtime.uninstall_workload_package(
            record,
            component_id=component_id,
            request_id="cyrene-uninstall-request-2",
            maintenance=hold,
            staging_root=tmp_path,
        )
