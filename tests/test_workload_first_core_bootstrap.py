"""First-Core source registration stays bound to the held workload identity."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "packaging" / "workload_package_runtime.py"
SPEC = importlib.util.spec_from_file_location("workload_first_core_bootstrap_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)


def _load_module(name: str, path: Path) -> Any:
    """Load one fixed sibling helper under a stable test-module identity."""

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


native_core = _load_module(
    "workload_first_core_native_bootstrap_test", ROOT / "packaging" / "native_core_bootstrap.py"
)
native_component_bootstrap = _load_module(
    "workload_first_core_component_bootstrap_test",
    ROOT / "packaging" / "native_component_bootstrap.py",
)
updates = _load_module(
    "workload_first_core_component_updates_test",
    ROOT / "packaging" / "component_updates.py",
)

SOURCE_ID = "cyrene-catalyst"
SOURCE_UID = 12001
SOURCE_GID = 12002
STANDALONE_SOURCE_ID = "cyrene-plugin-standalone-operator"
STANDALONE_SOURCE_UID = 12004
STANDALONE_SOURCE_GID = 12005
REQUEST_ID = "cyrene-first-core-catalog-001"
PLAN_ID = "plan-" + "a" * 32
MAINTENANCE_GID = 12003
TOKEN = "first-core-source-token"
CORE_COMPONENT_IDS = (
    "cyrene-linux-sys-adapter",
    "cyrene-nvidia-adapter",
    "cyrene-sandboxd",
    "cyrene-runtime-maintenance",
    "cyrene-kernel",
    "cy-package-runtime",
)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _source_policy() -> dict[str, Any]:
    return {
        "mode": "actualProduct",
        "productComponentIds": [SOURCE_ID],
        "productSources": [{"componentId": SOURCE_ID, "sourceId": SOURCE_ID}],
        "operations": list(runtime.SOURCE_OPERATIONS),
    }


def _selected_rows() -> list[dict[str, Any]]:
    source_policy = _source_policy()
    return [
        {
            "componentId": "cyrene-plugin-document-parsing",
            "packageId": "org.cyrene.document-parsing",
            "version": "0.2.0",
            "capabilityId": "org.cyrene.document-parsing",
            "bindingId": "document-parsing-catalyst",
            "packageArtifactDigest": _digest("document-parsing-package"),
            "sourcePolicy": source_policy,
        }
    ]


def _maintenance(*, request_id: str = REQUEST_ID) -> dict[str, Any]:
    components = {
        component_id: _digest(component_id) for component_id in (*CORE_COMPONENT_IDS, SOURCE_ID)
    }
    components.update(
        {row["componentId"]: row["packageArtifactDigest"] for row in _selected_rows()}
    )
    return {
        "transaction_id": request_id,
        "maintenance_token": "private-maintenance-token",
        "target_kind": "CORE_RUNTIME",
        "plan_id": PLAN_ID,
        "plan_digest": _digest("first-core-parent-plan"),
        "component_artifact_digests": components,
        "expected_gate_generation": 1,
        "expected_catalog_generation": 0,
    }


def _source_principals(token_path: Path) -> dict[str, dict[str, Any]]:
    return {SOURCE_ID: {"uid": SOURCE_UID, "gid": SOURCE_GID, "tokenPath": token_path}}


def _configure_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    catalog_path = tmp_path / "runtime" / "activity-sources.json"
    policy_path = tmp_path / "runtime" / "runtime-package-sources.json"
    token_directory = tmp_path / "tokens"
    staging_root = tmp_path / "staging"

    monkeypatch.setattr(runtime, "_require_root", lambda: None)
    monkeypatch.setattr(runtime, "_effective_uid", os.geteuid)
    monkeypatch.setattr(runtime, "_maintenance_group_id", lambda: MAINTENANCE_GID)
    monkeypatch.setattr(runtime, "_runtime_group_id", os.getgid)
    monkeypatch.setattr(runtime, "_validate_policy_parent", lambda *_args, **_kwargs: None)

    def ensure_private_dir(path: Path, *, parent: Path | None = None) -> None:
        del parent
        Path(path).mkdir(parents=True, mode=0o700, exist_ok=True)
        Path(path).chmod(0o700)

    def write_private_json(path: Path, value: Any) -> None:
        path = Path(path)
        content = _canonical(value)
        if path.exists():
            assert path.read_bytes() == content
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o600)

    def write_policy(
        policy: dict[str, Any], *, expected_prior_digest: str | None, policy_path: Path
    ) -> str:
        path = Path(policy_path)
        previous = path.read_bytes() if path.exists() else None
        previous_digest = (
            "sha256:" + hashlib.sha256(previous).hexdigest() if previous is not None else None
        )
        assert previous_digest == expected_prior_digest
        content = _canonical(policy)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o440)
        return "sha256:" + hashlib.sha256(content).hexdigest()

    monkeypatch.setattr(runtime, "_ensure_private_dir", ensure_private_dir)
    monkeypatch.setattr(runtime, "_write_private_json", write_private_json)
    monkeypatch.setattr(
        runtime,
        "_read_policy_bytes_optional",
        lambda path, **_kwargs: Path(path).read_bytes() if Path(path).exists() else None,
    )
    monkeypatch.setattr(runtime, "write_runtime_source_policy_cas", write_policy)
    monkeypatch.setattr(
        runtime,
        "read_runtime_source_policy_generic",
        lambda path: json.loads(Path(path).read_text(encoding="utf-8")),
    )
    monkeypatch.setattr(
        runtime,
        "_verify_first_core_catalog_metadata",
        lambda path, gid: _assert_first_core_catalog_file(path, gid, MAINTENANCE_GID),
    )
    bootstrap = runtime._bootstrap_module()
    monkeypatch.setattr(bootstrap, "PACKAGE_BOOTSTRAP_STAGE_ROOT", staging_root)
    return catalog_path, policy_path, token_directory


def _assert_first_core_catalog_file(path: Path, actual_gid: int, expected_gid: int) -> None:
    assert Path(path).is_file()
    assert actual_gid == expected_gid
    assert stat.S_IMODE(Path(path).stat().st_mode) == 0o640


def _init_catalog_runner(
    *,
    expected_maintenance_gid: int,
    token_value: str = TOKEN,
    order: list[str] | None = None,
) -> Any:
    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if order is not None:
            order.append("init-catalog")

        def option(name: str) -> str:
            return argv[argv.index(name) + 1]

        assert option("--catalog-gid") == str(expected_maintenance_gid)
        assert option("--source") == f"{SOURCE_ID}={SOURCE_UID}:{SOURCE_GID}"
        catalog_file = Path(option("--catalog"))
        token_dir = Path(option("--token-dir"))
        scope_map = json.loads(Path(option("--binding-scopes-json")).read_text(encoding="utf-8"))
        proof = json.loads(Path(option("--maintenance-proof-file")).read_text(encoding="utf-8"))
        assert proof["request_id"] == REQUEST_ID
        assert proof["plan_id"] == PLAN_ID
        assert proof["maintenance_token"] == "private-maintenance-token"
        assert proof["component_artifact_digests"] == _maintenance()["component_artifact_digests"]
        assert scope_map == {SOURCE_ID: []}

        token_dir.mkdir(parents=True, exist_ok=True)
        token_path = token_dir / f"{SOURCE_ID}.token"
        token_path.write_text(token_value + "\n", encoding="utf-8")
        token_path.chmod(0o400)
        token_digest = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
        catalog = {
            "schema_version": 1,
            "generation": 1,
            "sources": [
                {
                    "source_id": SOURCE_ID,
                    "uid": SOURCE_UID,
                    "gid": SOURCE_GID,
                    "source_token_sha256": token_digest,
                    "binding_scopes": scope_map[SOURCE_ID],
                }
            ],
        }
        catalog_file.parent.mkdir(parents=True, exist_ok=True)
        catalog_file.write_bytes(_canonical(catalog))
        catalog_file.chmod(0o640)
        receipt = {
            "schema_version": 1,
            "generation": 1,
            "sources": [
                {
                    "source_id": SOURCE_ID,
                    "token_file": str(token_path),
                    "binding_scope_count": 0,
                }
            ],
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(receipt), "")

    return runner


def _initialize(
    *,
    maintenance: dict[str, Any],
    source_principals: dict[str, dict[str, Any]],
    catalog_path: Path,
    policy_path: Path,
    token_directory: Path,
    runner: Any,
) -> dict[str, Any]:
    return runtime.initialize_first_core_activity_catalog(
        maintenance=maintenance,
        request_id=REQUEST_ID,
        source_policy=_source_policy(),
        selected_rows=_selected_rows(),
        source_principals=source_principals,
        activity_catalog_path=catalog_path,
        policy_path=policy_path,
        token_directory=token_directory,
        command=Path("/usr/bin/cyrene"),
        runner=runner,
    )


def test_first_core_registers_the_real_parent_source_and_recovers_exact_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path, policy_path, token_directory = _configure_runtime(monkeypatch, tmp_path)
    principals = _source_principals(token_directory / f"{SOURCE_ID}.token")
    calls: list[str] = []
    runner = _init_catalog_runner(expected_maintenance_gid=MAINTENANCE_GID, order=calls)

    first = _initialize(
        maintenance=_maintenance(),
        source_principals=principals,
        catalog_path=catalog_path,
        policy_path=policy_path,
        token_directory=token_directory,
        runner=runner,
    )

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    token_digest = hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()
    expected_source = {
        "source_id": SOURCE_ID,
        "uid": SOURCE_UID,
        "gid": SOURCE_GID,
        "source_token_sha256": token_digest,
        "binding_scopes": [],
    }
    assert catalog == {"schema_version": 1, "generation": 1, "sources": [expected_source]}
    assert policy == {
        "schema_version": 1,
        "generation": 1,
        "sources": [
            {
                "source_id": SOURCE_ID,
                "uid": SOURCE_UID,
                "gid": SOURCE_GID,
                "source_token_sha256": token_digest,
                "bindings": [],
            }
        ],
    }
    assert first["catalogGeneration"] == first["policyGeneration"] == 1
    assert first["sourceIdentities"] == [
        {"sourceId": SOURCE_ID, "uid": SOURCE_UID, "gid": SOURCE_GID}
    ]
    assert TOKEN not in json.dumps(first)
    assert calls == ["init-catalog"]

    resumed = _initialize(
        maintenance=_maintenance(),
        source_principals=principals,
        catalog_path=catalog_path,
        policy_path=policy_path,
        token_directory=token_directory,
        runner=lambda *_args, **_kwargs: pytest.fail(
            "an exact generation-one retry must resume from verified state"
        ),
    )
    assert resumed == first
    assert calls == ["init-catalog"]

    mismatched_catalog = {**catalog, "sources": [{**expected_source, "uid": SOURCE_UID + 1}]}
    catalog_path.write_bytes(_canonical(mismatched_catalog))
    catalog_path.chmod(0o640)
    mismatched_bytes = catalog_path.read_bytes()
    policy_bytes = policy_path.read_bytes()
    with pytest.raises(runtime.WorkloadPackageRuntimeError):
        _initialize(
            maintenance=_maintenance(),
            source_principals=principals,
            catalog_path=catalog_path,
            policy_path=policy_path,
            token_directory=token_directory,
            runner=lambda *_args, **_kwargs: pytest.fail(
                "a valid but unrelated generation-one source must not be adopted"
            ),
        )
    assert catalog_path.read_bytes() == mismatched_bytes
    assert policy_path.read_bytes() == policy_bytes


def test_first_core_registers_empty_standalone_owner_without_plugin_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path, policy_path, token_directory = _configure_runtime(monkeypatch, tmp_path)
    maintenance = _maintenance()
    maintenance["component_artifact_digests"] = {
        component_id: _digest(component_id) for component_id in CORE_COMPONENT_IDS
    }
    token_path = token_directory / f"{STANDALONE_SOURCE_ID}.token"
    source_policy = {
        "mode": "standaloneOperator",
        "productComponentIds": [],
        "productSources": [],
        "operations": list(runtime.SOURCE_OPERATIONS),
        "sourceId": STANDALONE_SOURCE_ID,
    }
    principals = {
        STANDALONE_SOURCE_ID: {
            "uid": STANDALONE_SOURCE_UID,
            "gid": STANDALONE_SOURCE_GID,
            "tokenPath": token_path,
        }
    }
    scope_maps: list[dict[str, list[Any]]] = []

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        def option(name: str) -> str:
            return argv[argv.index(name) + 1]

        assert option("--source") == (
            f"{STANDALONE_SOURCE_ID}={STANDALONE_SOURCE_UID}:{STANDALONE_SOURCE_GID}"
        )
        assert option("--catalog-gid") == str(MAINTENANCE_GID)
        proof = json.loads(Path(option("--maintenance-proof-file")).read_text(encoding="utf-8"))
        assert proof["component_artifact_digests"] == maintenance["component_artifact_digests"]
        scope_map = json.loads(Path(option("--binding-scopes-json")).read_text(encoding="utf-8"))
        assert scope_map == {STANDALONE_SOURCE_ID: []}
        scope_maps.append(scope_map)

        token_dir = Path(option("--token-dir"))
        token_dir.mkdir(parents=True, exist_ok=True)
        token_file = token_dir / f"{STANDALONE_SOURCE_ID}.token"
        token_file.write_text(TOKEN + "\n", encoding="utf-8")
        token_file.chmod(0o400)
        catalog_file = Path(option("--catalog"))
        catalog_file.parent.mkdir(parents=True, exist_ok=True)
        catalog_file.write_bytes(
            _canonical(
                {
                    "schema_version": 1,
                    "generation": 1,
                    "sources": [
                        {
                            "source_id": STANDALONE_SOURCE_ID,
                            "uid": STANDALONE_SOURCE_UID,
                            "gid": STANDALONE_SOURCE_GID,
                            "source_token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                            "binding_scopes": [],
                        }
                    ],
                }
            )
        )
        catalog_file.chmod(0o640)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "schema_version": 1,
                    "generation": 1,
                    "sources": [
                        {
                            "source_id": STANDALONE_SOURCE_ID,
                            "token_file": str(token_file),
                            "binding_scope_count": 0,
                        }
                    ],
                }
            ),
            "",
        )

    result = runtime.initialize_first_core_activity_catalog(
        maintenance=maintenance,
        request_id=REQUEST_ID,
        source_policy=source_policy,
        selected_rows=(),
        source_principals=principals,
        activity_catalog_path=catalog_path,
        policy_path=policy_path,
        token_directory=token_directory,
        command=Path("/usr/bin/cyrene"),
        runner=runner,
    )

    assert scope_maps == [{STANDALONE_SOURCE_ID: []}]
    assert result["catalogGeneration"] == result["policyGeneration"] == 1
    assert result["sourceIdentities"] == [
        {
            "sourceId": STANDALONE_SOURCE_ID,
            "uid": STANDALONE_SOURCE_UID,
            "gid": STANDALONE_SOURCE_GID,
        }
    ]
    assert (
        json.loads(catalog_path.read_text(encoding="utf-8"))["sources"][0]["binding_scopes"] == []
    )
    assert json.loads(policy_path.read_text(encoding="utf-8"))["sources"][0]["bindings"] == []


def test_standalone_empty_core_source_requires_held_package_runtime_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path, policy_path, token_directory = _configure_runtime(monkeypatch, tmp_path)
    maintenance = _maintenance()
    maintenance["component_artifact_digests"] = {
        component_id: _digest(component_id)
        for component_id in CORE_COMPONENT_IDS
        if component_id != "cy-package-runtime"
    }
    source_policy = {
        "mode": "standaloneOperator",
        "productComponentIds": [],
        "productSources": [],
        "operations": list(runtime.SOURCE_OPERATIONS),
        "sourceId": STANDALONE_SOURCE_ID,
    }
    principals = {
        STANDALONE_SOURCE_ID: {
            "uid": STANDALONE_SOURCE_UID,
            "gid": STANDALONE_SOURCE_GID,
            "tokenPath": token_directory / f"{STANDALONE_SOURCE_ID}.token",
        }
    }

    with pytest.raises(runtime.WorkloadPackageRuntimeError, match="cy-package-runtime identity"):
        runtime.initialize_first_core_activity_catalog(
            maintenance=maintenance,
            request_id=REQUEST_ID,
            source_policy=source_policy,
            selected_rows=(),
            source_principals=principals,
            activity_catalog_path=catalog_path,
            policy_path=policy_path,
            token_directory=token_directory,
            command=Path("/usr/bin/cyrene"),
            runner=lambda *_args, **_kwargs: pytest.fail(
                "an incomplete first-Core hold must fail before init-catalog"
            ),
        )


def test_first_core_recovers_the_existing_catalog_commit_policy_cas_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path, policy_path, token_directory = _configure_runtime(monkeypatch, tmp_path)
    principals = _source_principals(token_directory / f"{SOURCE_ID}.token")
    calls: list[str] = []
    writer = runtime.write_runtime_source_policy_cas

    def fail_policy_commit(*_args: Any, **_kwargs: Any) -> str:
        raise runtime.WorkloadPackageRuntimeError("simulated interruption before policy CAS")

    monkeypatch.setattr(runtime, "write_runtime_source_policy_cas", fail_policy_commit)
    with pytest.raises(runtime.WorkloadPackageRuntimeError, match="simulated interruption"):
        _initialize(
            maintenance=_maintenance(),
            source_principals=principals,
            catalog_path=catalog_path,
            policy_path=policy_path,
            token_directory=token_directory,
            runner=_init_catalog_runner(expected_maintenance_gid=MAINTENANCE_GID, order=calls),
        )

    catalog_bytes = catalog_path.read_bytes()
    token_path = token_directory / f"{SOURCE_ID}.token"
    token_bytes = token_path.read_bytes()
    assert not policy_path.exists()
    assert calls == ["init-catalog"]

    monkeypatch.setattr(runtime, "write_runtime_source_policy_cas", writer)
    resumed = _initialize(
        maintenance=_maintenance(),
        source_principals=principals,
        catalog_path=catalog_path,
        policy_path=policy_path,
        token_directory=token_directory,
        runner=lambda *_args, **_kwargs: pytest.fail(
            "the exact Broker generation-one commit must not be repeated"
        ),
    )

    assert resumed["catalogGeneration"] == resumed["policyGeneration"] == 1
    assert catalog_path.read_bytes() == catalog_bytes
    assert token_path.read_bytes() == token_bytes
    assert json.loads(policy_path.read_text(encoding="utf-8"))["sources"][0]["uid"] == SOURCE_UID
    assert calls == ["init-catalog"]


@pytest.mark.parametrize(
    "change",
    [
        lambda hold: hold.update(target_kind="PACKAGE_ONLY"),
        lambda hold: hold.update(transaction_id="another-first-core-request"),
        lambda hold: hold.update(expected_catalog_generation=1),
    ],
)
def test_first_core_rejects_a_hold_not_bound_to_fresh_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: Any
) -> None:
    catalog_path, policy_path, token_directory = _configure_runtime(monkeypatch, tmp_path)
    hold = _maintenance()
    change(hold)

    with pytest.raises(runtime.WorkloadPackageRuntimeError):
        _initialize(
            maintenance=hold,
            source_principals=_source_principals(token_directory / f"{SOURCE_ID}.token"),
            catalog_path=catalog_path,
            policy_path=policy_path,
            token_directory=token_directory,
            runner=lambda *_args, **_kwargs: pytest.fail(
                "an invalid CoreBootstrap hold must fail before init-catalog"
            ),
        )


@pytest.mark.parametrize("existing", [b"{not-json", b'{"schema_version":1}'])
def test_first_core_fails_closed_on_corrupt_or_unrelated_existing_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bytes,
) -> None:
    catalog_path, policy_path, token_directory = _configure_runtime(monkeypatch, tmp_path)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_bytes(existing)
    catalog_path.chmod(0o640)
    original = catalog_path.read_bytes()

    with pytest.raises(runtime.WorkloadPackageRuntimeError):
        _initialize(
            maintenance=_maintenance(),
            source_principals=_source_principals(token_directory / f"{SOURCE_ID}.token"),
            catalog_path=catalog_path,
            policy_path=policy_path,
            token_directory=token_directory,
            runner=lambda *_args, **_kwargs: pytest.fail(
                "existing state must be verified or rejected, never overwritten"
            ),
        )
    assert catalog_path.read_bytes() == original


def test_first_core_rejects_a_materialized_empty_generation_zero_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path, policy_path, token_directory = _configure_runtime(monkeypatch, tmp_path)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_bytes(_canonical({"schema_version": 1, "generation": 0, "sources": []}))
    catalog_path.chmod(0o640)
    original = catalog_path.read_bytes()

    with pytest.raises(runtime.WorkloadPackageRuntimeError):
        _initialize(
            maintenance=_maintenance(),
            source_principals=_source_principals(token_directory / f"{SOURCE_ID}.token"),
            catalog_path=catalog_path,
            policy_path=policy_path,
            token_directory=token_directory,
            runner=lambda *_args, **_kwargs: pytest.fail(
                "a materialized generation-zero file is not the missing-catalog first boot"
            ),
        )
    assert catalog_path.read_bytes() == original


def test_first_core_rejects_source_token_path_outside_its_catalog_token_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path, policy_path, token_directory = _configure_runtime(monkeypatch, tmp_path)

    with pytest.raises(runtime.WorkloadPackageRuntimeError, match="token path"):
        _initialize(
            maintenance=_maintenance(),
            source_principals=_source_principals(tmp_path / "other" / f"{SOURCE_ID}.token"),
            catalog_path=catalog_path,
            policy_path=policy_path,
            token_directory=token_directory,
            runner=lambda *_args, **_kwargs: pytest.fail(
                "the Broker token path must match the signed source principal"
            ),
        )


def test_first_core_checks_catalog_group_before_persisting_runtime_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path, policy_path, token_directory = _configure_runtime(monkeypatch, tmp_path)

    def reject_catalog_metadata(_path: Path, _gid: int) -> None:
        raise runtime.WorkloadPackageRuntimeError("unsafe first-Core Catalog metadata")

    monkeypatch.setattr(runtime, "_verify_first_core_catalog_metadata", reject_catalog_metadata)

    with pytest.raises(runtime.WorkloadPackageRuntimeError, match="unsafe first-Core"):
        _initialize(
            maintenance=_maintenance(),
            source_principals=_source_principals(token_directory / f"{SOURCE_ID}.token"),
            catalog_path=catalog_path,
            policy_path=policy_path,
            token_directory=token_directory,
            runner=_init_catalog_runner(expected_maintenance_gid=MAINTENANCE_GID),
        )

    assert catalog_path.is_file()
    assert not policy_path.exists()


@pytest.mark.parametrize(
    ("uid", "gid", "mode", "links", "symlink", "valid"),
    [
        (0, MAINTENANCE_GID, 0o640, 1, False, True),
        (1000, MAINTENANCE_GID, 0o640, 1, False, False),
        (0, MAINTENANCE_GID + 1, 0o640, 1, False, False),
        (0, MAINTENANCE_GID, 0o600, 1, False, False),
        (0, MAINTENANCE_GID, 0o640, 2, False, False),
        (0, MAINTENANCE_GID, 0o640, 1, True, False),
    ],
)
def test_first_core_catalog_metadata_requires_root_maintenance_group_and_0640(
    monkeypatch: pytest.MonkeyPatch,
    uid: int,
    gid: int,
    mode: int,
    links: int,
    symlink: bool,
    valid: bool,
) -> None:
    info = SimpleNamespace(
        st_mode=stat.S_IFREG | mode,
        st_uid=uid,
        st_gid=gid,
        st_nlink=links,
    )
    monkeypatch.setattr(Path, "lstat", lambda _path: info)
    monkeypatch.setattr(Path, "is_symlink", lambda _path: symlink)

    if valid:
        runtime._verify_first_core_catalog_metadata(Path("/unused/catalog.json"), MAINTENANCE_GID)
    else:
        with pytest.raises(runtime.WorkloadPackageRuntimeError):
            runtime._verify_first_core_catalog_metadata(
                Path("/unused/catalog.json"), MAINTENANCE_GID
            )


def test_fresh_workload_first_core_initializes_catalog_before_kernel_and_probes_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path, policy_path, token_directory = _configure_runtime(monkeypatch, tmp_path)
    events: list[str] = []
    lock_entries: list[str] = []
    target_id = "linux-ubuntu-24.04-x86_64-systemd"
    plan_digest = _digest("fresh-workload-first-core-plan")
    plan_id = "plan-" + plan_digest.removeprefix("sha256:")[:32]
    request_id = "first-core-bootstrap-" + plan_id.removeprefix("plan-")
    broker_plan_digest = _digest("offline-broker-bootstrap-plan")
    source_uid = os.geteuid()
    source_gid = os.getegid()
    assert source_uid > 0 and source_gid > 0
    source_policy = {
        "mode": "standaloneOperator",
        "productComponentIds": [],
        "productSources": [],
        "operations": list(runtime.SOURCE_OPERATIONS),
        "sourceId": STANDALONE_SOURCE_ID,
    }
    source_principals = {
        STANDALONE_SOURCE_ID: {
            "uid": source_uid,
            "gid": source_gid,
            "tokenPath": token_directory / f"{STANDALONE_SOURCE_ID}.token",
        }
    }
    state_root = tmp_path / "updater-state"
    install_root = tmp_path / "managed-install"
    archive_bytes = b"verified staged broker archive"
    archive_digest = "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
    archive_path = state_root / "staged" / plan_id / "cyrene-runtime-maintenance" / "archive.tar"
    archive_path.parent.mkdir(parents=True, mode=0o700)
    for directory in (state_root, state_root / "staged", state_root / "staged" / plan_id):
        directory.chmod(0o700)
    archive_path.parent.chmod(0o700)
    archive_path.write_bytes(archive_bytes)
    archive_path.chmod(0o600)

    staged_components: list[dict[str, Any]] = []
    for component_id in native_core.C10_FIRST_CORE_COMPONENT_IDS:
        artifact_digest = (
            archive_digest
            if component_id == "cyrene-runtime-maintenance"
            else _digest(f"{component_id}-artifact")
        )
        row = {
            "componentId": component_id,
            "version": "1.2.3",
            "manifestDigest": _digest(f"{component_id}-manifest"),
            "artifactDigest": artifact_digest,
            "targetId": target_id,
        }
        if component_id == "cyrene-runtime-maintenance":
            row["archivePath"] = archive_path
            row["manifest"] = {
                "schemaVersion": 2,
                "componentId": component_id,
                "version": row["version"],
                "manifestDigest": row["manifestDigest"],
                "artifact": {"sha256": artifact_digest, "sizeBytes": len(archive_bytes)},
            }
        staged_components.append(row)

    full_digests = {row["componentId"]: row["artifactDigest"] for row in staged_components}
    block = {
        "schemaVersion": 1,
        "cohortId": "C10",
        "planId": plan_id,
        "planDigest": plan_digest,
        "targetId": target_id,
        "catalogDigest": _digest("compiled-catalog"),
        "components": [
            {
                key: row[key]
                for key in (
                    "componentId",
                    "version",
                    "manifestDigest",
                    "artifactDigest",
                    "targetId",
                )
            }
            for row in staged_components
        ],
        "componentArtifactDigests": full_digests,
        "maintenanceComponentArtifactDigests": full_digests,
    }
    plan = {
        "planId": plan_id,
        "planDigest": plan_digest,
        "requestId": request_id,
        "gateGeneration": 1,
        "catalogGeneration": 0,
        "activitySources": [],
        "components": staged_components,
        "componentArtifactDigests": full_digests,
        "firstCoreBootstrap": block,
        "brokerBootstrapPlanDigest": broker_plan_digest,
    }
    parent_plan = {
        "channel": "stable",
        "firstCoreBootstrap": block,
        "resolution": {
            "sourcePolicy": source_policy,
            "selectedComponents": [],
            "planDigestMaterial": {
                "firstCoreBootstrapInternal": {
                    "brokerBootstrapPlanDigest": broker_plan_digest,
                }
            },
        },
    }
    manifest_bytes = _canonical(staged_components[3]["manifest"])
    broker_release = {
        "index_bytes": b"signed-index",
        "index_attestation_bytes": b"index-attestation",
        "manifest_bytes": manifest_bytes,
        "artifact_attestation_bytes": b"artifact-attestation",
        "target_id": target_id,
        "confirm_plan_digest": broker_plan_digest,
    }

    class FakeUpdater:
        def __init__(self) -> None:
            self.state_root = state_root
            self.install_root = install_root
            self.systemd_unit_dirs = (tmp_path / "systemd",)
            self.components = {
                item["componentId"]: {
                    "componentId": item["componentId"],
                    "systemdUnit": item["componentId"] + ".service",
                }
                for item in staged_components
            }

        @contextmanager
        def _exclusive_update_lock(self):
            lock_entries.append("acquired")
            yield

        def _broker_request(
            self, method: str, _params: dict[str, Any], *, request_id: str | None = None
        ) -> dict[str, Any]:
            events.append(f"broker:{method}")
            if method == "Health":
                return {
                    "status": "SERVING",
                    "protocol_version": "cyrene.runtime-maintenance.broker.v1",
                    "catalog_generation": 0,
                    "gate_generation": 1,
                    "core_bootstrap_eligible": True,
                }
            if method == "BeginCoreBootstrap":
                return {
                    "status": "MAINTENANCE_ACTIVE",
                    "maintenance_origin": "CORE_BOOTSTRAP",
                    "readiness_claimed": False,
                    "held": True,
                    "maintenance_token": "held-core-token",
                    "gate_generation": 1,
                }
            if method == "EndMaintenance":
                assert request_id is not None
                return {"status": "READY", "unlocked": True}
            if method == "ValidateMaintenanceHold":
                return {
                    "valid": True,
                    "request_id": _params["request_id"],
                    "target_kind": _params["target_kind"],
                    "plan_id": _params["plan_id"],
                    "plan_digest": _params["plan_digest"],
                    "component_artifact_digests": _params["component_artifact_digests"],
                    "component_id": _params["component_id"],
                    "artifact_digest": _params["artifact_digest"],
                    "gate_generation": _params["expected_gate_generation"],
                    "catalog_generation": _params["expected_catalog_generation"],
                }
            raise AssertionError(method)

        def _activity_catalog(
            self, *, allow_uninitialized: bool = False
        ) -> tuple[dict[str, Any], list[str]]:
            if allow_uninitialized and not catalog_path.exists():
                return {"schema_version": 1, "generation": 0, "sources": []}, []
            value = json.loads(catalog_path.read_text(encoding="utf-8"))
            return value, [row["source_id"] for row in value["sources"]]

        def _load_workload_package_runtime(self) -> Any:
            return runtime

        def _load_native_package_runtime_bootstrap(self) -> Any:
            updater = self

            class Probe:
                def probe_runtime_authority(
                    self,
                    catalog: dict[str, Any],
                    *,
                    expected_catalog_generation: int,
                    source_id: str,
                    expected_uid: int,
                    expected_gid: int,
                ) -> dict[str, Any]:
                    events.append("authority-probe")
                    assert catalog["generation"] == expected_catalog_generation == 1
                    source = catalog["sources"][0]
                    assert source["source_id"] == source_id == STANDALONE_SOURCE_ID
                    assert source["uid"] == expected_uid == source_uid
                    assert source["gid"] == expected_gid == source_gid
                    assert updater is self_outer
                    return {
                        "authority": "platform_package_runtime",
                        "protocol_version": "cy-package-runtime.control.v1",
                        "catalog_generation": 1,
                        "capabilities": ["cy-package-runtime.binding-operation-admission.v1"],
                    }

            return Probe()

        def _active_native_pointer_identity(self, _component_id: str) -> None:
            return None

        def _capture_active_versions(self, _components: list[dict[str, Any]]) -> list[Any]:
            return []

        def _reload_catalog_for_operation(self) -> None:
            return None

        def _require_authorized_process(self) -> None:
            return None

        def _ensure_state_root(self) -> Path:
            self.state_root.mkdir(parents=True, exist_ok=True)
            return self.state_root

        def runner(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert argv[:2] == ["systemctl", "daemon-reload"]
            events.append("daemon-reload")
            return subprocess.CompletedProcess(argv, 0, "", "")

        def _run_systemctl(self, operation: str, unit: str) -> None:
            component_id = next(
                key for key, value in self.components.items() if value["systemdUnit"] == unit
            )
            events.append(f"{operation}:{component_id}")

        def _wait_unit_active(self, _unit: str) -> None:
            return None

        def _health_transaction(self, _transaction: dict[str, Any]) -> None:
            return None

    self_outer: Any = None
    updater = FakeUpdater()
    self_outer = updater
    journal_records: dict[Path, dict[str, Any]] = {}

    def catalog_runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        def option(name: str) -> str:
            return argv[argv.index(name) + 1]

        assert option("--source") == (f"{STANDALONE_SOURCE_ID}={source_uid}:{source_gid}")
        assert option("--catalog-gid") == str(MAINTENANCE_GID)
        scope_map = json.loads(Path(option("--binding-scopes-json")).read_text(encoding="utf-8"))
        assert scope_map == {STANDALONE_SOURCE_ID: []}
        proof = json.loads(Path(option("--maintenance-proof-file")).read_text(encoding="utf-8"))
        assert proof["component_artifact_digests"] == full_digests
        assert proof["maintenance_token"] == "held-core-token"
        events.append("init-catalog")

        token_dir = Path(option("--token-dir"))
        token_dir.mkdir(parents=True, exist_ok=True)
        token_path = token_dir / f"{STANDALONE_SOURCE_ID}.token"
        token_path.write_text(TOKEN + "\n", encoding="utf-8")
        token_path.chmod(0o400)
        catalog_file = Path(option("--catalog"))
        catalog_file.parent.mkdir(parents=True, exist_ok=True)
        catalog_file.write_bytes(
            _canonical(
                {
                    "schema_version": 1,
                    "generation": 1,
                    "sources": [
                        {
                            "source_id": STANDALONE_SOURCE_ID,
                            "uid": source_uid,
                            "gid": source_gid,
                            "source_token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                            "binding_scopes": [],
                        }
                    ],
                }
            )
        )
        catalog_file.chmod(0o640)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "schema_version": 1,
                    "generation": 1,
                    "sources": [
                        {
                            "source_id": STANDALONE_SOURCE_ID,
                            "token_file": str(token_path),
                            "binding_scope_count": 0,
                        }
                    ],
                }
            ),
            "",
        )

    initialize = runtime.initialize_first_core_activity_catalog

    def initialize_with_test_paths(**kwargs: Any) -> dict[str, Any]:
        return initialize(
            **kwargs,
            activity_catalog_path=catalog_path,
            policy_path=policy_path,
            token_directory=token_directory,
            command=Path("/usr/bin/cyrene"),
            runner=catalog_runner,
        )

    monkeypatch.setattr(
        runtime, "initialize_first_core_activity_catalog", initialize_with_test_paths
    )
    monkeypatch.setattr(native_core, "_is_root", lambda: True)
    monkeypatch.setattr(
        native_core, "_fresh_workload_core_plan", lambda *_args: (plan, block, full_digests)
    )
    monkeypatch.setattr(
        native_core, "_fresh_workload_core_check_empty_host", lambda *_args, **_kwargs: None
    )

    def fresh_broker_health(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        events.append("broker-health")
        return (
            {
                "componentId": "cyrene-runtime-maintenance",
                "targetId": target_id,
                "version": "1.2.3",
                "manifestDigest": staged_components[3]["manifestDigest"],
                "artifactDigest": archive_digest,
                "pointerIdentity": (
                    f"1.2.3--{staged_components[3]['manifestDigest'].removeprefix('sha256:')}"
                ),
                "mainPid": "42",
                "executable": "/usr/lib/cyrene/fake-broker",
            },
            {
                "status": "SERVING",
                "protocol_version": "cyrene.runtime-maintenance.broker.v1",
                "catalog_generation": 0,
                "gate_generation": 1,
                "core_bootstrap_eligible": True,
                "capabilities": [
                    "cyrene.runtime-maintenance.state.v2",
                    "cyrene.runtime-maintenance.binding-operations.v1",
                ],
            },
        )

    monkeypatch.setattr(native_core, "_fresh_workload_core_broker_health", fresh_broker_health)
    monkeypatch.setattr(native_core, "_assert_fresh", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        native_core,
        "_write_unit",
        lambda _updater, component, _release: events.append(
            f"write-unit:{component['componentId']}"
        ),
    )
    monkeypatch.setattr(
        native_core, "_activate_core_cohort", lambda *_args: events.append("activate-core")
    )
    monkeypatch.setattr(native_core, "_verify_started_processes", lambda *_args: None)
    monkeypatch.setattr(native_core, "_verify_live_core_cohort", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        native_core,
        "_require_core_ready",
        lambda *_args: {
            "active_task_count": 0,
            "inflight_runtime_admission_count": 0,
            "active_worker_count": 0,
            "active_allocation_count": 0,
        },
    )
    monkeypatch.setattr(
        native_core, "_journal_path", lambda _updater: state_root / "first-core.json"
    )
    monkeypatch.setattr(
        native_core, "_plan_path", lambda _updater, _plan_id: state_root / "plan.json"
    )
    monkeypatch.setattr(
        native_core, "_read_private_json", lambda path: journal_records.get(Path(path))
    )
    monkeypatch.setattr(
        native_core,
        "_write_private_json",
        lambda _updater, path, value: journal_records.__setitem__(Path(path), copy.deepcopy(value)),
    )

    expected_release_path = (
        install_root
        / "components"
        / "cyrene-runtime-maintenance"
        / "releases"
        / f"1.2.3--{staged_components[3]['manifestDigest'].removeprefix('sha256:')}"
    )
    lease_holder: list[Any] = []

    def broker_activation(_updater: Any, *, lock_lease: Any, **inputs: Any) -> dict[str, Any]:
        assert lock_lease is lease_holder[0]
        assert inputs["artifact_bytes"] == archive_bytes
        events.append("broker-verifier")
        return {
            "status": "activated",
            "planDigest": broker_plan_digest,
            "releasePath": str(expected_release_path),
            "componentId": "cyrene-runtime-maintenance",
            "targetId": target_id,
            "version": "1.2.3",
            "manifestDigest": staged_components[3]["manifestDigest"],
            "artifactDigest": archive_digest,
        }

    monkeypatch.setattr(
        native_component_bootstrap,
        "bootstrap_verified_runtime_maintenance_under_lock",
        broker_activation,
    )

    with native_component_bootstrap.exclusive_update_lock(updater) as lease:
        lease_holder.append(lease)
        result = native_core.apply_fresh_workload_first_core(
            updater,
            parent_plan=parent_plan,
            staged_components=staged_components,
            broker_release=broker_release,
            source_policy=source_policy,
            selected_plugin_rows=[],
            source_principals=source_principals,
            channel="stable",
            lock_lease=lease,
            bootstrap_module=native_component_bootstrap,
        )

    assert lock_entries == ["acquired"]
    assert result["status"] == "installed"
    assert events.index("broker-health") < events.index("broker:BeginCoreBootstrap")
    assert events.index("broker:BeginCoreBootstrap") < events.index("init-catalog")
    assert events.index("init-catalog") < events.index("write-unit:cyrene-kernel")
    assert events.index("init-catalog") < events.index("start:cyrene-kernel")
    assert events.index("start:cyrene-kernel") < events.index("authority-probe")


def _public_first_core_block() -> dict[str, Any]:
    """Return the immutable public projection used by workload-plan tests."""

    component_ids = list(native_core.C10_FIRST_CORE_COMPONENT_IDS)
    component_digests = {component_id: _digest(component_id) for component_id in component_ids}
    return {
        "schemaVersion": 1,
        "cohortId": "C10",
        "planId": PLAN_ID,
        "planDigest": _digest("parent-plan-for-public-projection"),
        "targetId": "linux-ubuntu-24.04-x86_64-systemd",
        "catalogDigest": _digest("active-v2-catalog"),
        "components": [
            {
                "componentId": component_id,
                "version": "1.2.3",
                "manifestDigest": _digest(component_id + "-manifest"),
                "artifactDigest": component_digests[component_id],
                "targetId": "linux-ubuntu-24.04-x86_64-systemd",
            }
            for component_id in component_ids
        ],
        "componentArtifactDigests": component_digests,
        "maintenanceComponentArtifactDigests": component_digests,
    }


def _workload_updates_updater(tmp_path: Path) -> Any:
    """Construct the ordinary V2 updater for public workload-dispatch regressions."""

    return updates.ComponentUpdater(
        catalog_path=ROOT / "governance" / "component-catalog-v2.json",
        activity_catalog_path=tmp_path / "activity-sources.json",
        socket_path=tmp_path / "runtime-maintenance.sock",
        broker_path=tmp_path / "missing-broker",
        install_root=tmp_path / "install",
        state_root=tmp_path / "state",
        release_lock_path=tmp_path / "missing-release-lock.json",
        trusted_catalog_digest=None,
        load_active_catalog=False,
    )


def _first_core_candidate_set(
    updater: Any, monkeypatch: pytest.MonkeyPatch, *, plan_digest: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build six locally staged native candidates with the catalog's exact C10 pins."""

    group = next(
        item
        for item in updater.catalog["compatibilityGroups"]
        if item["groupId"] == "package-runtime-native-v1"
    )
    target = updater.targets[updates.WORKLOAD_FIRST_CORE_TARGET_ID]["target"]
    archive_by_uri: dict[str, bytes] = {}
    candidates: dict[str, Any] = {}
    selected_rows: list[dict[str, Any]] = []

    for component_id in updates.WORKLOAD_FIRST_CORE_COMPONENT_IDS:
        component = updater.components[component_id]
        executable = f"bin/{component_id}"
        unit_path = f"systemd/{component['systemdUnit']}"
        payload_files = {
            executable: f"#!/bin/sh\nexec /usr/bin/true # {component_id}\n".encode(),
            unit_path: f"[Unit]\nDescription={component_id}\n".encode(),
        }
        archive_buffer = io.BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
            for path, content in payload_files.items():
                member = tarfile.TarInfo(path)
                member.size = len(content)
                member.mode = 0o755 if path == executable else 0o644
                archive.addfile(member, io.BytesIO(content))
        archive_bytes = archive_buffer.getvalue()
        artifact_digest = "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
        tag = f"preview-{component_id}-" + "a" * 40
        archive_name = f"{component_id}-ubuntu-24.04.tar.gz"
        artifact_uri = (
            "https://github.com/DoHorizon-AI/Cyrene-Platform/releases/download/"
            f"{tag}/{archive_name}"
        )
        file_map = {
            path: "sha256:" + hashlib.sha256(content).hexdigest()
            for path, content in payload_files.items()
        }
        manifest: dict[str, Any] = {
            "schemaVersion": 2,
            "componentId": component_id,
            "version": "0.1.0",
            "releaseId": tag,
            "target": target,
            "artifact": {
                "kind": "native-binary",
                "uri": artifact_uri,
                "sha256": artifact_digest,
                "sizeBytes": len(archive_bytes),
                "entrypoint": executable,
                "executableFiles": [executable],
                "files": file_map,
            },
            "source": {
                "repository": "DoHorizon-AI/Cyrene-Platform",
                "ref": "refs/heads/develop",
                "commit": "a" * 40,
            },
            "provenance": {"attestation": {"subjectName": archive_name}},
        }
        if component_id in {
            "cyrene-runtime-maintenance",
            "cyrene-kernel",
            "cy-package-runtime",
        }:
            manifest["protocolVersion"] = component["protocolVersion"]
            manifest["compatibility"] = {
                "groupId": group["groupId"],
                "groupVersion": group["groupVersion"],
                "contractApiVersion": group["contractApiVersion"],
                "wireApiVersion": group["wireApiVersion"],
                "contractLock": group["contractLock"],
            }
        manifest["manifestDigest"] = updates._digest_json(manifest, "manifestDigest")
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        manifest_asset_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        index_identity = {
            "assetName": "component-release-index-v1.json",
            "assetDigest": _digest("c10-index-" + component_id),
        }
        publisher_identity = {
            "id": "official-platform",
            "repository": "DoHorizon-AI/Cyrene-Platform",
        }
        attestation_ref = {
            "repository": "DoHorizon-AI/Cyrene-Platform",
            "workflow": updater.publishers["DoHorizon-AI/Cyrene-Platform"]["workflow"],
            "sourceCommit": "a" * 40,
            "subjectName": archive_name,
            "subjectDigest": artifact_digest,
        }
        selected_rows.append(
            {
                "componentId": component_id,
                "version": manifest["version"],
                "manifestDigest": manifest["manifestDigest"],
                "manifestAssetDigest": manifest_asset_digest,
                "artifactDigest": artifact_digest,
                "digest": artifact_digest,
                "targetId": updates.WORKLOAD_FIRST_CORE_TARGET_ID,
                "releaseId": tag,
                "indexIdentity": index_identity,
                "publisherIdentity": publisher_identity,
                "attestationRef": attestation_ref,
            }
        )
        candidates[component_id] = updates.Candidate(
            component=component,
            manifest=manifest,
            manifest_digest=manifest["manifestDigest"],
            artifact_digest=artifact_digest,
            manifest_uri=(
                "https://github.com/DoHorizon-AI/Cyrene-Platform/releases/download/"
                f"{tag}/component-release-manifest-v2.json"
            ),
            index={},
            index_uri=(
                "https://github.com/DoHorizon-AI/Cyrene-Platform/releases/download/"
                f"{tag}/component-release-index-v1.json"
            ),
            manifest_bytes=manifest_bytes,
            release_tag=tag,
            index_asset_name="component-release-index-v1.json",
            index_asset_digest=index_identity["assetDigest"],
            manifest_asset_digest=manifest_asset_digest,
        )
        archive_by_uri[artifact_uri] = archive_bytes

    monkeypatch.setattr(
        updater,
        "_get_release_asset_bytes",
        lambda _assets, uri, **_kwargs: archive_by_uri[uri],
    )
    # Candidate verification is an upstream precondition; exercise the installed payload
    # revalidator below without making this local filesystem test claim signature evidence.
    monkeypatch.setattr(updater, "_release_attestation_bundle", lambda **_kwargs: b"verified")
    return (
        {
            **_public_first_core_block(),
            "planId": PLAN_ID,
            "planDigest": plan_digest,
            "components": selected_rows,
        },
        candidates,
    )


def _simulate_root_owned_first_core_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Model root ownership only within this isolated temporary test tree."""

    original_lstat = Path.lstat
    original_fstat = os.fstat
    test_root = tmp_path.resolve()

    def root_owned_lstat(path: Path) -> os.stat_result:
        metadata = original_lstat(path)
        if not path.absolute().is_relative_to(test_root):
            return metadata
        fields = list(metadata)
        fields[4] = 0
        fields[5] = 0
        return os.stat_result(fields)

    def root_owned_fstat(descriptor: int) -> os.stat_result:
        metadata = original_fstat(descriptor)
        try:
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            return metadata
        if not descriptor_path.absolute().is_relative_to(test_root):
            return metadata
        fields = list(metadata)
        fields[4] = 0
        fields[5] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", root_owned_lstat)
    monkeypatch.setattr(updates.os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "fstat", root_owned_fstat)


@pytest.mark.parametrize("tamper_payload", [False, True])
def test_first_core_revalidates_payload_with_native_component_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper_payload: bool
) -> None:
    _simulate_root_owned_first_core_tree(monkeypatch, tmp_path)
    updater = _workload_updates_updater(tmp_path)
    plan_digest = _digest("first-core-stage-payload-verifier")
    plan_root = updater._private_state_directory("staged") / "workload-plans"
    plan_root.mkdir(mode=0o700)
    stage_root = plan_root / PLAN_ID
    stage_root.mkdir(mode=0o700)
    block, candidates = _first_core_candidate_set(updater, monkeypatch, plan_digest=plan_digest)

    staged = updater._first_core_stage_items(
        block,
        candidates,
        [],
        stage_root,
        plan_id=PLAN_ID,
        plan_digest=plan_digest,
    )
    if tamper_payload:
        release_path = Path(staged[0]["releasePath"])
        entrypoint = release_path / "bin" / staged[0]["componentId"]
        entrypoint.write_bytes(b"changed after the signed manifest was staged\n")

    if not tamper_payload:
        validated = updater._validate_first_core_stage_items(
            block,
            candidates,
            staged,
            plan_id=PLAN_ID,
            plan_digest=plan_digest,
            core_module=native_core,
        )
        assert validated == staged
        return

    with pytest.raises(updates.UpdateError, match="Staged C10 payload is unsafe") as error:
        updater._validate_first_core_stage_items(
            block,
            candidates,
            staged,
            plan_id=PLAN_ID,
            plan_digest=plan_digest,
            core_module=native_core,
        )
    assert error.value.code == "INVALID_STAGE"
    assert isinstance(error.value.__cause__, ValueError)
    assert "payload digest differs from the manifest" in str(error.value.__cause__)


@pytest.mark.parametrize(
    ("error_code", "message", "retryable"),
    [
        (
            "ATTESTATION_INVALID",
            "No acceptable GitHub attestation bundle was returned.",
            False,
        ),
        ("NETWORK_ERROR", "The signed release endpoint is unavailable.", True),
    ],
)
def test_first_core_candidate_failure_keeps_component_identity_in_check_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    message: str,
    retryable: bool,
) -> None:
    updater = _workload_updates_updater(tmp_path)
    target_id = updates.WORKLOAD_FIRST_CORE_TARGET_ID
    failing_component_id = "cyrene-kernel"
    plan_digest = _digest("first-core-candidate-failure")
    ready_resolution = {
        "status": "ready",
        "planId": "plan-" + plan_digest.removeprefix("sha256:")[:32],
        "planDigest": plan_digest,
        "action": "install",
        "channel": "preview",
        "selectedComponents": [],
        "warnings": [],
        "blockers": [],
        "planDigestMaterial": {"action": "install", "blockers": []},
    }

    resolver = SimpleNamespace(
        potential_component_ids=lambda *_args: ("cyrene-catalyst",),
        resolve_workload=lambda *_args, **_kwargs: SimpleNamespace(
            to_dict=lambda: copy.deepcopy(ready_resolution)
        ),
    )

    def candidate(
        component: dict[str, Any],
        target: dict[str, Any],
        _channel: str,
        *,
        release_id: str | None = None,
    ) -> SimpleNamespace:
        assert release_id is None
        component_id = component["componentId"]
        if component_id == failing_component_id:
            raise updates.UpdateError(error_code, message, retryable)
        return SimpleNamespace(manifest={"target": target["target"]})

    monkeypatch.setattr(updater, "_reload_catalog_for_operation", lambda: None)
    monkeypatch.setattr(updater, "_ensure_state_root", lambda: tmp_path)
    monkeypatch.setattr(
        updater, "_require_workload_target", lambda workload, target: (workload, target)
    )
    monkeypatch.setattr(updater, "_resolve_channel", lambda _channel: "preview")
    monkeypatch.setattr(updater, "_load_workload_resolver", lambda: resolver)
    monkeypatch.setattr(
        updater, "_workload_target_preference", lambda *_args: updates.WORKLOAD_HOST_TARGET
    )
    monkeypatch.setattr(
        updater,
        "_target_for",
        lambda _component, *, target_id=None: {"id": target_id, "target": target_id},
    )
    monkeypatch.setattr(updater, "_candidate", candidate)
    monkeypatch.setattr(updater, "_trusted_release_indexes", lambda _candidates: {"indexes": []})
    monkeypatch.setattr(
        updater,
        "_installed_workload_components",
        lambda _component_ids, *, workload_id=None: (
            {},
            {"components": {}, "installationRecords": {}, "sourceBindings": []},
        ),
    )
    monkeypatch.setattr(updater, "_first_core_host_is_unprovisioned", lambda: True)
    monkeypatch.setattr(
        updater,
        "_load_native_core_bootstrap",
        lambda: SimpleNamespace(_validate_package_runtime_group=lambda _updater: None),
    )

    result = updater.check_workload(
        "catalyst",
        updates.WORKLOAD_HOST_TARGET,
        {"includeComponentIds": [], "excludeComponentIds": [], "choices": {}},
        channel="preview",
    )

    blocker = next(item for item in result["blockers"] if item["code"] == error_code)
    assert result["status"] == "blocked"
    assert blocker["componentId"] == failing_component_id
    assert blocker["targetId"] == target_id
    assert blocker["message"] == message
    assert blocker["retryable"] is retryable
    assert blocker["details"] == {"phase": "firstCoreBootstrap"}


def test_first_core_public_projection_keeps_nine_fields_and_adds_check_stage_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _workload_updates_updater(tmp_path)
    block = _public_first_core_block()
    public_fields = set(block)
    assert public_fields == {
        "schemaVersion",
        "cohortId",
        "planId",
        "planDigest",
        "targetId",
        "catalogDigest",
        "components",
        "componentArtifactDigests",
        "maintenanceComponentArtifactDigests",
    }
    plan_id = block["planId"]
    plan_digest = block["planDigest"]
    sdk_id = updates.WORKLOAD_SDK_COMPONENT_ID
    sdk_row = {
        "componentId": sdk_id,
        "artifactKind": "python-bundle",
        "digest": _digest("sdk-release"),
        "version": "1.0.0",
    }
    resolution = {
        "status": "ready",
        "planId": plan_id,
        "planDigest": plan_digest,
        "action": "install",
        "channel": "stable",
        "selectedComponents": [sdk_row],
        "firstCoreBootstrap": copy.deepcopy(block),
        "warnings": [],
        "blockers": [],
    }
    monkeypatch.setattr(updater, "_reload_catalog_for_operation", lambda: None)
    monkeypatch.setattr(updater, "_ensure_state_root", lambda: tmp_path)
    monkeypatch.setattr(
        updater, "_require_workload_target", lambda workload, target: (workload, target)
    )
    monkeypatch.setattr(updater, "_resolve_channel", lambda _channel: "stable")
    monkeypatch.setattr(
        updater,
        "_build_workload_plan",
        lambda *_args, **_kwargs: (copy.deepcopy(resolution), {}, {}),
    )
    monkeypatch.setattr(updater, "_workload_plan_directory", lambda: tmp_path / "plans")
    persisted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        updates, "_atomic_json", lambda _path, value: persisted.append(copy.deepcopy(value))
    )

    checked = updater.check_workload("catalyst", block["targetId"], {}, channel="stable")

    assert checked["firstCoreBootstrap"]["status"] == "required"
    assert {key for key in checked["firstCoreBootstrap"] if key != "status"} == public_fields
    assert persisted[0]["firstCoreBootstrap"] == block
    assert set(persisted[0]["firstCoreBootstrap"]) == public_fields

    staged_sdk = {"componentId": sdk_id, "status": "staged", "artifactKind": "python-bundle"}
    broker_row = {"componentId": "cyrene-runtime-maintenance", "artifactDigest": _digest("broker")}
    staged_plan = {
        "planKind": updates.WORKLOAD_PROTOCOL_VERSION,
        "planId": plan_id,
        "planDigest": plan_digest,
        "catalogDigest": updater.catalog_digest,
        "catalogGeneration": updater.catalog_generation,
        "workloadId": "catalyst",
        "targetId": block["targetId"],
        "action": "install",
        "channel": "stable",
        "selections": {},
        "phase": "staged",
        "firstCoreBootstrap": copy.deepcopy(block),
        "stagedComponents": [staged_sdk],
        "firstCoreBootstrapStage": {
            "schemaVersion": 1,
            "parentPlanId": plan_id,
            "parentPlanDigest": plan_digest,
            "stagedComponents": [broker_row],
            "brokerProofs": {},
        },
    }
    monkeypatch.setattr(updates, "_read_object", lambda *_args: staged_plan)
    monkeypatch.setattr(updater, "_require_authorized_process", lambda: None)
    monkeypatch.setattr(updater, "_clear_release_discovery_caches", lambda: None)
    monkeypatch.setattr(updater, "_validate_plan_identity", lambda *_args: None)
    inventory = {"firstCoreCandidates": {"cyrene-runtime-maintenance": object()}}
    monkeypatch.setattr(
        updater,
        "_build_workload_plan",
        lambda *_args, **_kwargs: (copy.deepcopy(resolution), {}, inventory),
    )
    monkeypatch.setattr(
        updater,
        "_validate_cached_workload_stage_rows",
        lambda *_args, **_kwargs: ([staged_sdk], False),
    )
    monkeypatch.setattr(
        updater, "_validate_first_core_stage_items", lambda *_args, **_kwargs: [broker_row]
    )
    monkeypatch.setattr(updater, "_read_first_core_broker_release", lambda *_args, **_kwargs: {})

    staged = updater.stage_workload(
        "catalyst",
        block["targetId"],
        plan_id,
        plan_digest,
        action="install",
        channel="stable",
    )

    assert staged["firstCoreBootstrap"]["status"] == "staged"
    assert {
        key for key in staged["firstCoreBootstrap"] if key not in {"status", "stagedComponents"}
    } == public_fields
    assert staged["firstCoreBootstrap"]["stagedComponents"] == [broker_row]
    assert set(staged_plan["firstCoreBootstrap"]) == public_fields


def test_public_apply_initializes_first_core_before_sdk_prepare_and_normal_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _workload_updates_updater(tmp_path)
    block = _public_first_core_block()
    events: list[str] = []
    lease = object()
    atomic_json = updates._atomic_json
    bootstrap_module = SimpleNamespace()

    @contextmanager
    def exclusive_update_lock(_updater: Any):
        events.append("update-lock")
        yield lease

    bootstrap_module.exclusive_update_lock = exclusive_update_lock
    updater.catalog = {
        "workloads": [
            {
                "workloadId": "catalyst",
                "sourcePolicy": {
                    "mode": "actualProduct",
                    "productComponentIds": ["cyrene-catalyst"],
                    "productSources": [
                        {"componentId": "cyrene-catalyst", "sourceId": "cyrene-catalyst"}
                    ],
                    "operations": [],
                },
            }
        ]
    }
    source_policy = updater.catalog["workloads"][0]["sourcePolicy"]
    sdk_id = updates.WORKLOAD_SDK_COMPONENT_ID
    sdk_digest = _digest("workload-sdk-artifact")
    sdk_row = {
        "componentId": sdk_id,
        "artifactKind": "python-bundle",
        "digest": sdk_digest,
        "version": "1.0.0",
    }
    resolution = {
        "status": "ready",
        "planId": block["planId"],
        "planDigest": block["planDigest"],
        "action": "install",
        "channel": "stable",
        "selectedComponents": [sdk_row],
        "firstCoreBootstrap": copy.deepcopy(block),
        "sourcePolicy": copy.deepcopy(source_policy),
        "warnings": [],
        "blockers": [],
    }
    sdk_candidate = updates.Candidate(
        component={},
        manifest={},
        manifest_digest=_digest("sdk-manifest"),
        artifact_digest=sdk_digest,
        manifest_uri="https://example.invalid/sdk.json",
        index={},
        index_uri="https://example.invalid/index.json",
    )
    broker_candidate = updates.Candidate(
        component={},
        manifest={},
        manifest_digest=_digest("broker-manifest"),
        artifact_digest=_digest("broker-artifact"),
        manifest_uri="https://example.invalid/broker.json",
        index={},
        index_uri="https://example.invalid/index.json",
    )
    broker_stage = {"componentId": "cyrene-runtime-maintenance"}
    stored = {
        "planKind": updates.WORKLOAD_PROTOCOL_VERSION,
        "phase": "staged",
        "planId": block["planId"],
        "planDigest": block["planDigest"],
        "workloadId": "catalyst",
        "targetId": block["targetId"],
        "action": "install",
        "channel": "stable",
        "catalogDigest": updater.catalog_digest,
        "catalogGeneration": updater.catalog_generation,
        "selections": {},
        "firstCoreBootstrap": copy.deepcopy(block),
        "stagedComponents": [
            {"componentId": sdk_id, "artifactKind": "python-bundle", "status": "staged"}
        ],
        "firstCoreBootstrapStage": {
            "schemaVersion": 1,
            "parentPlanId": block["planId"],
            "parentPlanDigest": block["planDigest"],
            "stagedComponents": [broker_stage],
            "brokerProofs": {},
        },
    }
    inventory = {
        "firstCoreCandidates": {"cyrene-runtime-maintenance": broker_candidate},
        "installationRecords": {},
        "sourceBindings": [],
    }
    monkeypatch.setattr(updater, "_require_authorized_process", lambda: None)
    monkeypatch.setattr(updater, "_clear_release_discovery_caches", lambda: None)
    monkeypatch.setattr(updater, "_load_native_component_bootstrap", lambda: bootstrap_module)
    monkeypatch.setattr(updater, "_validate_plan_identity", lambda *_args: None)
    monkeypatch.setattr(
        updater, "_require_workload_target", lambda workload, target: (workload, target)
    )
    monkeypatch.setattr(updater, "_resolve_channel", lambda _channel: "stable")
    monkeypatch.setattr(updater, "_workload_plan_directory", lambda: tmp_path / "plans")
    monkeypatch.setattr(updates, "_read_object", lambda *_args: stored)
    monkeypatch.setattr(
        updater,
        "_build_workload_plan",
        lambda *_args, **_kwargs: (resolution, {sdk_id: sdk_candidate}, inventory),
    )
    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    monkeypatch.setattr(updater, "_private_state_directory", lambda name: tmp_path / name)
    monkeypatch.setattr(updater, "_capture_active_versions", lambda *_args: [])
    monkeypatch.setattr(
        updater, "_workload_component_artifact_map", lambda *_args: {sdk_id: sdk_digest}
    )
    monkeypatch.setattr(updater, "_workload_service_units", lambda *_args: [])
    monkeypatch.setattr(updater, "_load_workload_sdk_environment", lambda: object())
    monkeypatch.setattr(
        updater, "_activity_catalog", lambda **_kwargs: ({"generation": 0, "sources": []}, [])
    )
    monkeypatch.setattr(
        updater,
        "_workload_source_principals",
        lambda *_args: {"cyrene-catalyst": {"uid": 1, "gid": 1}},
    )
    monkeypatch.setattr(
        updater, "_validate_first_core_stage_items", lambda *_args, **_kwargs: [broker_stage]
    )
    monkeypatch.setattr(updater, "_read_first_core_broker_release", lambda *_args, **_kwargs: {})

    def record_atomic_json(path: Path, value: dict[str, Any], **kwargs: Any) -> None:
        if value.get("firstCoreBootstrapStatus") == "pending":
            events.append("durable-first-core-intent")
        atomic_json(path, value, **kwargs)

    monkeypatch.setattr(updates, "_atomic_json", record_atomic_json)

    def run_first_core(
        parent_plan: dict[str, Any],
        _staged_components: list[dict[str, Any]],
        _broker_release: dict[str, Any],
        _source_policy: dict[str, Any],
        _source_principals: dict[str, dict[str, Any]],
        *,
        lock_lease: Any,
        bootstrap_module: Any,
    ) -> dict[str, Any]:
        assert lock_lease is lease
        assert bootstrap_module is updater._load_native_component_bootstrap()
        assert set(parent_plan["firstCoreBootstrap"]) == set(block)
        events.append("first-core")
        return {
            "status": "installed",
            "planId": block["planId"],
            "planDigest": block["planDigest"],
            "catalogGeneration": 1,
            "readiness": {"status": "READY", "catalogGeneration": 1},
            "componentStatuses": [],
        }

    monkeypatch.setattr(updater, "_run_fresh_workload_first_core", run_first_core)
    monkeypatch.setattr(
        updater,
        "_load_workload_sdk_environment",
        lambda: SimpleNamespace(read_workload_sdk_environment=lambda: None),
    )

    def prepare_sdk(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        events.append("sdk-prepare")
        return {}

    monkeypatch.setattr(updater, "_prepare_workload_sdk_durably", prepare_sdk)
    monkeypatch.setattr(
        updates,
        "DEFAULT_PACKAGE_RUNTIME_POLICY",
        tmp_path / "no-existing-runtime-policy.json",
    )

    class NormalReadinessReached(Exception):
        pass

    def normal_core_phase(*_args: Any, **_kwargs: Any) -> bool:
        events.append("normal-core-readiness")
        raise NormalReadinessReached

    monkeypatch.setattr(updater, "_ensure_workload_phase", normal_core_phase)

    with pytest.raises(NormalReadinessReached):
        updater.apply_workload(
            "catalyst",
            block["targetId"],
            block["planId"],
            block["planDigest"],
            {"planId": block["planId"], "planDigest": block["planDigest"], "confirmed": True},
            action="install",
            channel="stable",
        )

    assert events.index("update-lock") < events.index("durable-first-core-intent")
    assert events.index("durable-first-core-intent") < events.index("first-core")
    assert events.index("first-core") < events.index("sdk-prepare")
    assert events.index("first-core") < events.index("normal-core-readiness")


def test_validate_maintenance_hold_uses_v1_wire_protocol(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "runtime-maintenance"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    observed: dict[str, Any] = {}

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        observed.update(payload)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({"request_id": payload["request_id"], "result": {"valid": True}}),
            "",
        )

    updater = updates.ComponentUpdater(
        catalog_path=ROOT / "governance" / "component-catalog-v2.json",
        activity_catalog_path=tmp_path / "activity-sources.json",
        socket_path=tmp_path / "runtime.sock",
        broker_path=executable,
        state_root=tmp_path / "state",
        install_root=tmp_path / "install",
        trusted_catalog_digest=None,
        load_active_catalog=False,
        runner=runner,
    )

    result = updater._broker_request(
        "ValidateMaintenanceHold",
        {"request_id": "first-core-request", "target_kind": "CORE_RUNTIME"},
        request_id="first-core-request",
    )

    assert result == {"valid": True}
    assert observed["protocol_version"] == "cyrene.runtime-maintenance.broker.v1"
    assert observed["method"] == "ValidateMaintenanceHold"
