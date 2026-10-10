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
import time
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


@pytest.mark.parametrize(
    ("gate_generation", "eligible", "expected", "require_eligible", "uncertain", "accepted"),
    [
        (0, True, 0, True, False, True),
        (0, True, None, True, False, False),
        (0, False, 0, True, False, False),
        (0, True, 0, False, False, False),
        (True, True, 0, True, False, False),
        (-1, True, 0, True, False, False),
        (1, True, 0, True, False, False),
        (1, True, True, True, False, False),
        (1, False, 1, False, False, True),
        (1, True, None, True, False, True),
        (0, True, 0, False, True, True),
        (1, False, 0, False, True, True),
        (0, False, 0, False, True, False),
        (1, True, 0, False, True, False),
        (2, False, 0, False, True, False),
        (0, True, 0, True, True, False),
    ],
)
def test_fresh_core_broker_health_generation_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    gate_generation: int | bool,
    eligible: Any,
    expected: int | bool | None,
    require_eligible: bool,
    uncertain: bool,
    accepted: bool,
) -> None:
    manifest_digest = _digest("health-broker-manifest")
    artifact_digest = _digest("health-broker-archive")
    broker_row = {
        "version": "1.2.3",
        "manifestDigest": manifest_digest,
        "artifactDigest": artifact_digest,
    }
    broker_identity = {
        "pointerIdentity": f"1.2.3--{manifest_digest.removeprefix('sha256:')}",
        "manifestDigest": manifest_digest,
        "artifactDigest": artifact_digest,
    }
    health = {
        "status": "SERVING",
        "protocol_version": "cyrene.runtime-maintenance.broker.v1",
        "catalog_generation": 0,
        "gate_generation": gate_generation,
        "core_bootstrap_eligible": eligible,
        "capabilities": [
            "cyrene.runtime-maintenance.state.v2",
            "cyrene.runtime-maintenance.binding-operations.v1",
        ],
    }
    updater = SimpleNamespace(
        _broker_request=lambda method, params: (
            health
            if method == "Health" and params == {}
            else pytest.fail("Health validation issued an unexpected Broker request")
        )
    )
    monkeypatch.setattr(
        native_core,
        "_verified_running_c10_broker",
        lambda _updater, _proc_root: broker_identity,
    )
    if accepted:
        broker, result = native_core._fresh_workload_core_broker_health(
            updater,
            broker_row,
            proc_root=Path("/proc"),
            expected_gate_generation=expected,
            require_eligible=require_eligible,
            allow_begin_result_uncertain=uncertain,
        )
        assert broker is broker_identity
        assert result is health
    else:
        with pytest.raises(RuntimeError, match="does not prove generation-zero eligibility"):
            native_core._fresh_workload_core_broker_health(
                updater,
                broker_row,
                proc_root=Path("/proc"),
                expected_gate_generation=expected,
                require_eligible=require_eligible,
                allow_begin_result_uncertain=uncertain,
            )


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
            self, method: str, params: dict[str, Any], *, request_id: str | None = None
        ) -> dict[str, Any]:
            events.append(f"broker:{method}")
            if method == "Health":
                events.append("broker-health")
                return {
                    "status": "SERVING",
                    "protocol_version": "cyrene.runtime-maintenance.broker.v1",
                    "catalog_generation": 0,
                    "gate_generation": 0,
                    "core_bootstrap_eligible": True,
                    "capabilities": [
                        "cyrene.runtime-maintenance.state.v2",
                        "cyrene.runtime-maintenance.binding-operations.v1",
                    ],
                }
            if method == "BeginCoreBootstrap":
                assert params["expected_gate_generation"] == 0
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
                    "request_id": params["request_id"],
                    "target_kind": params["target_kind"],
                    "plan_id": params["plan_id"],
                    "plan_digest": params["plan_digest"],
                    "component_artifact_digests": params["component_artifact_digests"],
                    "component_id": params["component_id"],
                    "artifact_digest": params["artifact_digest"],
                    "gate_generation": params["expected_gate_generation"],
                    "catalog_generation": params["expected_catalog_generation"],
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

    broker_identity = {
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
    }
    monkeypatch.setattr(
        native_core,
        "_verified_running_c10_broker",
        lambda _updater, _proc_root: broker_identity,
    )
    monkeypatch.setattr(
        native_core,
        "_fresh_workload_core_recover_held_broker",
        lambda *_args, **_kwargs: events.append("held-broker-recovery"),
    )
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
    assert events.index("init-catalog") < events.index("held-broker-recovery")
    assert events.index("held-broker-recovery") < events.index("broker:ValidateMaintenanceHold")
    assert events.index("init-catalog") < events.index("write-unit:cyrene-kernel")
    assert events.index("init-catalog") < events.index("start:cyrene-kernel")
    assert events.index("start:cyrene-kernel") < events.index("authority-probe")


def _held_broker_recovery_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    initial_state: tuple[str, str, str, str] = ("inactive", "dead", "0", "0"),
    startup_failure: bool = False,
    orphan_process: bool = False,
) -> dict[str, Any]:
    """Build a held first-Core journal with seams for exact Broker resume tests."""

    broker_id = "cyrene-runtime-maintenance"
    target_id = "linux-ubuntu-24.04-x86_64-systemd"
    manifest_digest = _digest("held-broker-manifest")
    artifact_digest = _digest("held-broker-artifact")
    pointer = f"1.2.3--{manifest_digest.removeprefix('sha256:')}"
    unit = f"{broker_id}.service"
    executable = "/usr/lib/cyrene/releases/held-broker"
    previous_identity = {
        "componentId": broker_id,
        "pointerIdentity": pointer,
        "version": "1.2.3",
        "manifestDigest": manifest_digest,
        "artifactDigest": artifact_digest,
        "systemdUnit": unit,
        "mainPid": "42" if initial_state[0] == "active" else "41",
        "executable": executable,
    }
    plan_digest = _digest("held-first-core-plan")
    broker_plan_digest = _digest("held-offline-broker-plan")
    source_id = STANDALONE_SOURCE_ID
    source_uid = 12004
    source_gid = 12005
    full_digests = {
        component_id: _digest(component_id)
        for component_id in native_core.C10_FIRST_CORE_COMPONENT_IDS
    }
    full_digests.update(
        {f"test-plugin-{index}": _digest(f"test-plugin-{index}") for index in range(4)}
    )
    plan = {
        "planId": "plan-" + plan_digest.removeprefix("sha256:")[:32],
        "planDigest": plan_digest,
        "requestId": "first-core-bootstrap-held-test",
        "brokerBootstrapPlanDigest": broker_plan_digest,
        "firstCoreBootstrap": {
            "targetId": target_id,
            "catalogDigest": _digest("held-compiled-catalog"),
        },
    }
    transaction = {
        "phase": "hold_required",
        "progress": "catalog_initialized",
        "maintenanceToken": "private-held-token-123",
        "maintenanceGateGeneration": 1,
        "bootstrapBroker": dict(previous_identity),
    }
    broker_row = {
        "componentId": broker_id,
        "targetId": target_id,
        "version": "1.2.3",
        "manifestDigest": manifest_digest,
        "artifactDigest": artifact_digest,
    }
    events: list[Any] = []
    current = {"active": initial_state[0] == "active"}
    current_pid = {"value": initial_state[2] if initial_state[0] == "active" else "4243"}
    proc_root = tmp_path / "proc"
    for pid in {previous_identity["mainPid"], "4243"}:
        process = proc_root / pid
        process.mkdir(parents=True)
        (process / "status").write_text(
            "Name:\tcyrene-runtime-maintenance\nUid:\t0\t0\t0\t0\nGid:\t0\t0\t0\t0\n",
            encoding="ascii",
        )
    journal_path = tmp_path / "private" / "first-core.json"
    written: list[dict[str, Any]] = []

    def broker_request(
        method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        events.append(("broker", method, params))
        if method == "Health":
            return {
                "status": "SERVING",
                "protocol_version": "cyrene.runtime-maintenance.broker.v1",
                "catalog_generation": 1,
                "gate_generation": 1,
                "core_bootstrap_eligible": False,
                "capabilities": [
                    "cyrene.runtime-maintenance.state.v2",
                    "cyrene.runtime-maintenance.binding-operations.v1",
                ],
            }
        if method == "ValidateMaintenanceHold":
            return {
                "valid": True,
                "request_id": params["request_id"],
                "target_kind": params["target_kind"],
                "plan_id": params["plan_id"],
                "plan_digest": params["plan_digest"],
                "component_artifact_digests": params["component_artifact_digests"],
                "component_id": params["component_id"],
                "artifact_digest": params["artifact_digest"],
                "gate_generation": params["expected_gate_generation"],
                "catalog_generation": params["expected_catalog_generation"],
            }
        raise AssertionError(method)

    def run_systemctl(operation: str, actual_unit: str) -> None:
        events.append(("start", operation, actual_unit))
        assert operation == "start" and actual_unit == unit
        if startup_failure:
            raise RuntimeError(f"systemctl failure includes {transaction['maintenanceToken']}")
        current["active"] = True

    updater = SimpleNamespace(
        components={broker_id: {"componentId": broker_id, "systemdUnit": unit}},
        install_root=tmp_path / "install",
        _active_native_pointer_identity=lambda _component: pointer,
        _activity_catalog=lambda **_kwargs: (
            {
                "generation": 1,
                "sources": [
                    {
                        "source_id": source_id,
                        "uid": source_uid,
                        "gid": source_gid,
                        "binding_scopes": [],
                    }
                ],
            },
            [source_id],
        ),
        _broker_request=broker_request,
        _run_systemctl=run_systemctl,
        _wait_unit_active=lambda actual_unit: events.append(("wait", actual_unit)),
    )
    activation = {
        "status": "activated",
        "planDigest": broker_plan_digest,
        "releasePath": str(updater.install_root / "components" / broker_id / "releases" / pointer),
        "componentId": broker_id,
        "targetId": target_id,
        "version": "1.2.3",
        "manifestDigest": manifest_digest,
        "artifactDigest": artifact_digest,
    }
    monkeypatch.setattr(
        native_core,
        "_candidate_executable",
        lambda *_args: Path(executable),
    )
    monkeypatch.setattr(
        native_core,
        "_fresh_workload_core_verify_broker_activation",
        lambda *_args, **_kwargs: events.append("activation-proof") or activation,
    )
    monkeypatch.setattr(
        native_core,
        "_prove_candidate_unit_loaded",
        lambda _updater, _row, **kwargs: (
            events.append(("unit-proof", kwargs["restore_missing"])) or unit
        ),
    )
    monkeypatch.setattr(
        native_core, "_candidate_startup_clock", lambda _updater: (time.monotonic, time.sleep)
    )

    def unit_state(_updater: Any, actual_unit: str, **_kwargs: Any) -> tuple[str, str, str, str]:
        assert actual_unit == unit
        if current["active"]:
            return "active", "running", current_pid["value"], "0"
        return initial_state

    monkeypatch.setattr(native_core, "_candidate_unit_state", unit_state)
    monkeypatch.setattr(
        native_core,
        "_systemd_unit_property",
        lambda _updater, _unit, property_name, **_kwargs: (
            "root" if property_name in {"User", "Group"} else ""
        ),
    )

    def verified_broker(_updater: Any, _proc_root: Path) -> dict[str, Any]:
        return {
            **{key: value for key, value in previous_identity.items() if key != "mainPid"},
            "mainPid": current_pid["value"],
        }

    monkeypatch.setattr(native_core, "_verified_running_c10_broker", verified_broker)
    monkeypatch.setattr(
        native_core,
        "_write_private_json",
        lambda _updater, _path, value: written.append(copy.deepcopy(value)),
    )
    bootstrap_module = SimpleNamespace(
        _broker_process_exists=lambda _root: events.append("process-table") or orphan_process
    )
    return {
        "activation": activation,
        "bootstrap_module": bootstrap_module,
        "broker_inputs": {},
        "broker_row": broker_row,
        "events": events,
        "full_digests": full_digests,
        "journal_path": journal_path,
        "plan": plan,
        "previous_identity": previous_identity,
        "proc_root": proc_root,
        "source_gid": source_gid,
        "source_id": source_id,
        "source_uid": source_uid,
        "transaction": transaction,
        "updater": updater,
        "written": written,
    }


def _transaction_validation_case() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build the original generation-zero Begin binding with its held generation one."""

    source_id = STANDALONE_SOURCE_ID
    source_policy = {"mode": "standaloneOperator", "sourceId": source_id}
    source_principals = {source_id: {"uid": 12004, "gid": 12005}}
    component_digests = {"cyrene-runtime-maintenance": _digest("held-broker")}
    block = {"schemaVersion": 1, "cohortId": "C10", "catalogDigest": _digest("catalog")}
    plan = {
        "planId": "plan-held-validation",
        "planDigest": _digest("parent-held-validation"),
        "requestId": "first-core-bootstrap-held-validation",
        "gateGeneration": 0,
        "catalogGeneration": 0,
        "activitySources": [],
        "componentArtifactDigests": component_digests,
        "firstCoreBootstrap": block,
        "components": [],
        "brokerBootstrapPlanDigest": _digest("nested-broker-plan"),
    }
    transaction = {
        "mode": "fresh-workload-first-core",
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "requestId": plan["requestId"],
        "expectedCatalogGeneration": 0,
        "expectedActivitySources": [],
        "componentArtifactDigests": component_digests,
        "firstCoreBootstrap": block,
        "components": [],
        "sourcePolicy": source_policy,
        "selectedPluginRows": [],
        "sourcePrincipalIdentities": source_principals,
        "brokerBootstrapPlanDigest": plan["brokerBootstrapPlanDigest"],
        "expectedGateGeneration": 0,
        "beginRequest": native_core._core_bootstrap_begin_request(plan),
        "phase": "hold_required",
        "progress": "catalog_initialized",
        "maintenanceGateGeneration": 1,
    }
    return (
        plan,
        transaction,
        {
            "source_policy": source_policy,
            "source_principals": source_principals,
        },
    )


def test_held_transaction_accepts_original_zero_begin_and_generation_one_hold() -> None:
    plan, transaction, inputs = _transaction_validation_case()

    native_core._fresh_workload_core_validate_transaction(
        transaction,
        plan,
        source_policy=inputs["source_policy"],
        selected_plugin_rows=[],
        source_principals=inputs["source_principals"],
    )

    assert plan["gateGeneration"] == 0
    assert transaction["expectedGateGeneration"] == 0
    assert transaction["maintenanceGateGeneration"] == 1
    assert transaction["beginRequest"] == native_core._core_bootstrap_begin_request(plan)


@pytest.mark.parametrize("mutation", ["negative", "boolean", "begin-request"])
def test_held_transaction_rejects_invalid_zero_begin_binding(mutation: str) -> None:
    plan, transaction, inputs = _transaction_validation_case()
    if mutation == "negative":
        transaction["expectedGateGeneration"] = -1
    elif mutation == "boolean":
        transaction["expectedGateGeneration"] = True
    else:
        transaction["beginRequest"]["expected_gate_generation"] = 1

    with pytest.raises(ValueError, match="live gate snapshot"):
        native_core._fresh_workload_core_validate_transaction(
            transaction,
            plan,
            source_policy=inputs["source_policy"],
            selected_plugin_rows=[],
            source_principals=inputs["source_principals"],
        )


def test_held_resume_validates_only_after_recovery_and_recovers_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _held_broker_recovery_case(monkeypatch, tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        native_core,
        "_fresh_workload_core_recover_held_broker",
        lambda *_args, **_kwargs: events.append("recover"),
    )
    monkeypatch.setattr(
        native_core,
        "_fresh_workload_core_validate_maintenance_hold",
        lambda *_args, **_kwargs: events.append("validate"),
    )
    arguments = {
        "source_id": case["source_id"],
        "source_uid": case["source_uid"],
        "source_gid": case["source_gid"],
        "lock_lease": object(),
        "proc_root": case["proc_root"],
        "journal_path": case["journal_path"],
    }
    recovered = native_core._fresh_workload_core_validate_held_transaction(
        case["updater"],
        case["bootstrap_module"],
        case["plan"],
        case["transaction"],
        case["broker_row"],
        case["broker_inputs"],
        case["full_digests"],
        **arguments,
        recover_broker=True,
    )
    native_core._fresh_workload_core_validate_held_transaction(
        case["updater"],
        case["bootstrap_module"],
        case["plan"],
        case["transaction"],
        case["broker_row"],
        case["broker_inputs"],
        case["full_digests"],
        **arguments,
        recover_broker=not recovered,
    )

    assert recovered is True
    assert events == ["recover", "validate", "validate"]


def test_held_broker_recovery_starts_exact_inactive_unit_and_reuses_same_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _held_broker_recovery_case(monkeypatch, tmp_path)
    identity = native_core._fresh_workload_core_recover_held_broker(
        case["updater"],
        case["bootstrap_module"],
        case["plan"],
        case["transaction"],
        case["broker_row"],
        case["broker_inputs"],
        lock_lease=object(),
        proc_root=case["proc_root"],
        journal_path=case["journal_path"],
    )
    catalog_generation = native_core._fresh_workload_core_validate_maintenance_hold(
        case["updater"],
        case["plan"],
        case["transaction"],
        case["full_digests"],
        source_id=case["source_id"],
        source_uid=case["source_uid"],
        source_gid=case["source_gid"],
    )

    requests = [
        event for event in case["events"] if isinstance(event, tuple) and event[0] == "broker"
    ]
    validations = [event[2] for event in requests if event[1] == "ValidateMaintenanceHold"]
    assert identity["mainPid"] == "4243"
    assert catalog_generation == 1
    assert case["transaction"]["phase"] == "hold_required"
    assert case["transaction"]["progress"] == "catalog_initialized"
    assert case["transaction"]["maintenanceToken"] == "private-held-token-123"
    assert [
        event[:2] for event in case["events"] if isinstance(event, tuple) and event[0] == "start"
    ] == [("start", "start")]
    assert len(validations) == len(case["full_digests"]) == 10
    assert {request["component_id"] for request in validations} == set(case["full_digests"])
    assert all(request["request_id"] == case["plan"]["requestId"] for request in validations)
    assert all(request["plan_id"] == case["plan"]["planId"] for request in validations)
    assert all(request["plan_digest"] == case["plan"]["planDigest"] for request in validations)
    assert all(request["maintenance_token"] == "private-held-token-123" for request in validations)
    assert all(request["expected_gate_generation"] == 1 for request in validations)
    assert all(request["expected_catalog_generation"] == 1 for request in validations)
    assert all(
        request["component_artifact_digests"] == case["full_digests"] for request in validations
    )
    assert not any(event[1] == "BeginCoreBootstrap" for event in requests)
    assert case["written"][-1]["bootstrapBroker"]["mainPid"] == "4243"
    assert "brokerRecoveryError" not in case["written"][-1]


def test_held_broker_recovery_leaves_exact_active_broker_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = ("active", "running", "42", "0")
    case = _held_broker_recovery_case(monkeypatch, tmp_path, initial_state=active)
    identity = native_core._fresh_workload_core_recover_held_broker(
        case["updater"],
        case["bootstrap_module"],
        case["plan"],
        case["transaction"],
        case["broker_row"],
        case["broker_inputs"],
        lock_lease=object(),
        proc_root=case["proc_root"],
        journal_path=case["journal_path"],
    )

    assert identity == case["previous_identity"]
    assert not any(isinstance(event, tuple) and event[0] == "start" for event in case["events"])
    assert "process-table" not in case["events"]
    assert case["transaction"]["maintenanceToken"] == "private-held-token-123"


@pytest.mark.parametrize("mismatch", ["journal", "pointer"])
def test_held_broker_recovery_refuses_mismatched_identity_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    case = _held_broker_recovery_case(monkeypatch, tmp_path)
    if mismatch == "journal":
        case["transaction"]["bootstrapBroker"]["artifactDigest"] = _digest("tampered")
    else:
        case["updater"]._active_native_pointer_identity = lambda _component: "different-pointer"

    with pytest.raises(RuntimeError, match="exact maintenance hold remains available"):
        native_core._fresh_workload_core_recover_held_broker(
            case["updater"],
            case["bootstrap_module"],
            case["plan"],
            case["transaction"],
            case["broker_row"],
            case["broker_inputs"],
            lock_lease=object(),
            proc_root=case["proc_root"],
            journal_path=case["journal_path"],
        )

    assert not any(isinstance(event, tuple) and event[0] == "start" for event in case["events"])
    assert case["transaction"]["phase"] == "hold_required"
    assert case["transaction"]["progress"] == "catalog_initialized"
    assert case["transaction"]["maintenanceToken"] == "private-held-token-123"


def test_held_broker_start_failure_keeps_redacted_recoverable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _held_broker_recovery_case(monkeypatch, tmp_path, startup_failure=True)

    with pytest.raises(RuntimeError, match="exact maintenance hold remains available"):
        native_core._fresh_workload_core_recover_held_broker(
            case["updater"],
            case["bootstrap_module"],
            case["plan"],
            case["transaction"],
            case["broker_row"],
            case["broker_inputs"],
            lock_lease=object(),
            proc_root=case["proc_root"],
            journal_path=case["journal_path"],
        )

    saved = case["written"][-1]
    error_text = json.dumps(saved["brokerRecoveryError"])
    assert case["transaction"]["phase"] == "hold_required"
    assert case["transaction"]["progress"] == "catalog_initialized"
    assert case["transaction"]["maintenanceToken"] == "private-held-token-123"
    assert "private-held-token-123" not in error_text
    assert "[REDACTED]" in error_text
    assert len(error_text) < 1000


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


def _held_sdk_recovery_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str = "pending",
    current_sdk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one exact staged held transaction for SDK-ordering recovery tests."""

    updater = _workload_updates_updater(tmp_path)
    block = _public_first_core_block()
    plan_id = block["planId"]
    plan_digest = block["planDigest"]
    policy = {
        "mode": "actualProduct",
        "productComponentIds": ["cyrene-catalyst"],
        "productSources": [{"componentId": "cyrene-catalyst", "sourceId": "cyrene-catalyst"}],
        "operations": [],
    }
    updater.catalog = {"workloads": [{"workloadId": "catalyst", "sourcePolicy": policy}]}
    sdk_id = updates.WORKLOAD_SDK_COMPONENT_ID
    target_id = updates.WORKLOAD_SDK_TARGET_ID
    archive = tmp_path / "sdk.tar.gz"
    archive.write_bytes(b"verified SDK archive")
    bundle = tmp_path / "sdk-bundle"
    bundle.mkdir()
    wheel = bundle / "cyrene_runtime_maintenance-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"verified SDK wheel")
    sdk_row = {
        "componentId": sdk_id,
        "artifactKind": "python-bundle",
        "version": "0.1.0",
        "manifestDigest": _digest("sdk-manifest"),
        "manifestAssetDigest": _digest("sdk-manifest-asset"),
        "digest": updates._file_digest(archive),
        "releaseId": "sdk-release-1",
        "targetId": target_id,
        "indexIdentity": {"releaseTag": "sdk-release-1"},
        "publisherIdentity": {"repository": "DoHorizon-AI/Cyrene-Workspace"},
        "attestationRef": {"subjectName": archive.name},
        "manifestUri": "https://example.invalid/sdk-release.json",
        "requiredness": "required",
    }
    sdk_stage_identity = {
        "archivePath": str(archive),
        "bundlePath": str(bundle),
        "wheelPath": str(wheel),
        "wheelDigest": _digest("sdk-wheel"),
        "planId": plan_id,
        "planDigest": plan_digest,
    }
    sdk_stage = {
        **{field: sdk_row[field] for field in updates.WORKLOAD_STAGE_RESOLUTION_IDENTITY_FIELDS},
        "componentId": sdk_id,
        "status": "staged",
        "artifactKind": "python-bundle",
        "stagedIdentity": sdk_stage_identity,
    }
    plugin_id = "cyrene-plugin-document-parsing"
    plugin_row = {
        "componentId": plugin_id,
        "artifactKind": "plugin-package",
        "version": "0.2.0",
        "digest": _digest("plugin-release"),
        "manifestDigest": _digest("plugin-manifest"),
        "manifestAssetDigest": _digest("plugin-manifest-asset"),
        "releaseId": "plugin-release-1",
        "targetId": "linux-ubuntu-24.04-x86_64-plugin",
        "indexIdentity": {"releaseTag": "plugin-release-1"},
        "publisherIdentity": {"repository": "DoHorizon-AI/Cyrene-Plugins-Official"},
        "attestationRef": {"subjectName": "plugin.json"},
        "sourcePolicy": policy,
        "requiredness": "recommended",
    }
    plugin_stage = {
        **{field: plugin_row[field] for field in updates.WORKLOAD_STAGE_RESOLUTION_IDENTITY_FIELDS},
        "componentId": plugin_id,
        "status": "staged",
        "artifactKind": "plugin-package",
        "packageArtifactDigest": _digest("plugin-package-aggregate"),
    }
    selected_rows = [plugin_row, sdk_row]
    staged_rows = [plugin_stage, sdk_stage]
    resolution = {
        "status": "ready",
        "planId": plan_id,
        "planDigest": plan_digest,
        "action": "install",
        "channel": "stable",
        "selectedComponents": copy.deepcopy(selected_rows),
        "firstCoreBootstrap": copy.deepcopy(block),
        "sourcePolicy": copy.deepcopy(policy),
        "planDigestMaterial": {"firstCoreBootstrap": copy.deepcopy(block)},
    }
    stored = {
        "planKind": updates.WORKLOAD_PROTOCOL_VERSION,
        "phase": "staged",
        "planId": plan_id,
        "planDigest": plan_digest,
        "workloadId": "catalyst",
        "targetId": block["targetId"],
        "action": "install",
        "channel": "stable",
        "catalogDigest": updater.catalog_digest,
        "catalogGeneration": updater.catalog_generation,
        "selections": {},
        "candidates": {
            sdk_id: {
                "manifestUri": sdk_row["manifestUri"],
                "manifestDigest": sdk_row["manifestDigest"],
                "manifestAssetDigest": sdk_row["manifestAssetDigest"],
                "artifactDigest": sdk_row["digest"],
                "releaseTag": sdk_row["releaseId"],
                "targetId": target_id,
            }
        },
        "resolution": copy.deepcopy(resolution),
        "firstCoreBootstrap": copy.deepcopy(block),
        "stagedComponents": copy.deepcopy(staged_rows),
        "firstCoreBootstrapStage": {
            "schemaVersion": 1,
            "parentPlanId": plan_id,
            "parentPlanDigest": plan_digest,
            "stagedComponents": [{"componentId": "cyrene-runtime-maintenance"}],
            "brokerProofs": {},
        },
    }
    component_digests = updater._workload_component_artifact_map(
        selected_rows, {row["componentId"]: row for row in staged_rows}, {"components": {}}
    )
    result = {
        "status": "installed",
        "planId": plan_id,
        "planDigest": plan_digest,
        "catalogGeneration": 1,
        "readiness": {"status": "READY", "catalogGeneration": 1},
        "componentStatuses": [],
    }
    transaction = {
        "transactionKind": "workload-assembly.v1",
        "action": "install",
        "planId": plan_id,
        "planDigest": plan_digest,
        "workloadId": "catalyst",
        "targetId": block["targetId"],
        "catalogDigest": updater.catalog_digest,
        "channel": "stable",
        "componentArtifactDigests": component_digests,
        "firstCoreBootstrap": copy.deepcopy(block),
        "firstCoreBootstrapStatus": status,
        "firstCoreBootstrapResult": copy.deepcopy(result) if status == "installed" else None,
        "resolution": copy.deepcopy(resolution),
        "selectedComponents": copy.deepcopy(selected_rows),
        "stagedComponents": copy.deepcopy(staged_rows),
        "sourcePolicy": copy.deepcopy(policy),
        "sdkEnvironment": None,
        "phase": "applying",
    }
    transaction_path = tmp_path / "transactions" / f"{plan_id}.json"
    transaction_path.parent.mkdir()
    transaction_path.parent.chmod(0o700)
    updates._atomic_json(transaction_path, transaction)
    events: list[str] = []
    core_candidate = updates.Candidate(
        component={"componentId": "cyrene-runtime-maintenance"},
        manifest={},
        manifest_digest=_digest("broker-manifest"),
        artifact_digest=_digest("broker-artifact"),
        manifest_uri="https://example.invalid/broker.json",
        index={},
        index_uri="https://example.invalid/index.json",
    )
    core_journal = {
        "mode": "fresh-workload-first-core",
        "planId": block["planId"],
        "planDigest": block["planDigest"],
        "firstCoreBootstrap": copy.deepcopy(block),
        "componentArtifactDigests": copy.deepcopy(block["maintenanceComponentArtifactDigests"]),
        "phase": "hold_required" if status == "pending" else "succeeded",
        "progress": "catalog_initialized" if status == "pending" else "complete",
    }
    sdk_candidate = updates.Candidate(
        component={"componentId": sdk_id},
        manifest={"version": sdk_row["version"], "target": {"platform": "linux"}},
        manifest_digest=sdk_row["manifestDigest"],
        artifact_digest=sdk_row["digest"],
        manifest_uri=sdk_row["manifestUri"],
        index={},
        index_uri="https://example.invalid/sdk-index.json",
        release_tag=sdk_row["releaseId"],
        manifest_asset_digest=sdk_row["manifestAssetDigest"],
    )
    sdk_state: dict[str, Any] = {"identity": current_sdk}
    sdk_module = SimpleNamespace(
        WORKLOAD_OPERATOR_ROOT=tmp_path / "missing-operator-root",
        read_workload_sdk_environment=lambda: sdk_state["identity"],
        _validate_candidate=lambda _selected, identity: {
            "archivePath": Path(identity["archivePath"]),
            "bundlePath": Path(identity["bundlePath"]),
            "wheelPath": Path(identity["wheelPath"]),
            "wheelDigest": identity["wheelDigest"],
        },
        _require_root_file=lambda *_args, **_kwargs: None,
        _require_root_directory=lambda *_args, **_kwargs: None,
        _require_root_directory_chain=lambda *_args, **_kwargs: None,
        _read_verified_wheel=lambda *_args, **_kwargs: None,
    )

    def prepare(_component: dict[str, Any], _identity: dict[str, Any]) -> None:
        events.append("sdk-prepare")
        sdk_state["identity"] = {"installed": True}

    sdk_module.prepare_workload_sdk_environment = prepare
    monkeypatch.setattr(updater, "_load_workload_sdk_environment", lambda: sdk_module)
    monkeypatch.setattr(
        updater,
        "_load_native_core_bootstrap",
        lambda: SimpleNamespace(
            _journal_path=lambda _updater: tmp_path / "first-core-bootstrap.json",
            _read_private_json=lambda _path: core_journal,
        ),
    )
    monkeypatch.setattr(updater, "_candidate", lambda *_args, **_kwargs: sdk_candidate)
    monkeypatch.setattr(
        updater,
        "_first_core_resolution_projection",
        lambda projected, _candidates, **_kwargs: (
            (projected["firstCoreBootstrap"], {"cyrene-runtime-maintenance": core_candidate})
        ),
    )
    monkeypatch.setattr(
        updater,
        "_validate_first_core_stage_items",
        lambda *_args, **_kwargs: [{"componentId": "cyrene-runtime-maintenance"}],
    )
    monkeypatch.setattr(updater, "_read_first_core_broker_release", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        updater, "_activity_catalog", lambda **_kwargs: ({"generation": 1, "sources": []}, [])
    )
    monkeypatch.setattr(updater, "_workload_source_principals", lambda *_args: {})
    monkeypatch.setattr(
        updater,
        "_run_fresh_workload_first_core",
        lambda *_args, **_kwargs: events.append("core-resume") or copy.deepcopy(result),
    )
    monkeypatch.setattr(
        updater,
        "_workload_sdk_matches_selected",
        lambda _selected, installed: (
            isinstance(installed, dict) and installed.get("installed") is True
        ),
    )
    atomic_json = updates._atomic_json

    def record_atomic_json(path: Path, value: dict[str, Any], **kwargs: Any) -> None:
        intent = value.get("sdkPrepareIntent")
        if isinstance(intent, dict) and intent.get("status") == "pending":
            events.append("sdk-intent")
        atomic_json(path, value, **kwargs)

    monkeypatch.setattr(updates, "_atomic_json", record_atomic_json)
    monkeypatch.setattr(updates, "DEFAULT_PACKAGE_RUNTIME_POLICY", tmp_path / "runtime-policy.json")
    updates.DEFAULT_PACKAGE_RUNTIME_POLICY.write_text("{}", encoding="utf-8")
    updater.components[sdk_id] = {"componentId": sdk_id}
    updater.targets[target_id] = {"id": target_id, "target": {"platform": "linux"}}
    return {
        "updater": updater,
        "block": block,
        "resolution": resolution,
        "stored": stored,
        "transaction": transaction,
        "transaction_path": transaction_path,
        "policy": policy,
        "events": events,
        "result": result,
        "sdk_row": sdk_row,
        "sdk_stage": sdk_stage,
    }


@pytest.mark.parametrize("status", ["pending", "installed"])
def test_preinventory_resume_recovers_pending_and_post_core_pre_intent_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    case = _held_sdk_recovery_case(tmp_path, monkeypatch, status=status)
    updater = case["updater"]

    result, sdk_prepared = updater._resume_first_core_before_workload_inventory(
        case["stored"],
        case["transaction"],
        case["transaction_path"],
        lock_lease=object(),
        bootstrap_module=object(),
    )

    assert result == case["result"]
    assert sdk_prepared is True
    assert case["events"] == ["core-resume", "sdk-intent", "sdk-prepare"]
    saved = updates._read_object(case["transaction_path"], "transaction")
    assert saved["firstCoreBootstrapStatus"] == "installed"
    assert saved["sdkPrepareIntent"]["status"] == "prepared"


@pytest.mark.parametrize("mutation", ["plan", "staged_sdk", "component_map", "source", "hold"])
def test_preinventory_resume_rejects_changed_authority_before_core_or_sdk_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    case = _held_sdk_recovery_case(tmp_path, monkeypatch)
    if mutation == "plan":
        case["transaction"]["planDigest"] = _digest("changed-plan")
    elif mutation == "staged_sdk":
        case["stored"]["stagedComponents"][1]["stagedIdentity"]["planDigest"] = _digest(
            "changed-sdk-stage"
        )
    elif mutation == "component_map":
        case["transaction"]["componentArtifactDigests"][updates.WORKLOAD_SDK_COMPONENT_ID] = (
            _digest("changed-component-map")
        )
    elif mutation == "source":
        case["transaction"]["sourcePolicy"] = {"mode": "tampered"}
    else:
        case["transaction"]["firstCoreBootstrap"]["planDigest"] = _digest("changed-hold")

    with pytest.raises(updates.UpdateError):
        case["updater"]._resume_first_core_before_workload_inventory(
            case["stored"],
            case["transaction"],
            case["transaction_path"],
            lock_lease=object(),
            bootstrap_module=object(),
        )

    assert case["events"] == []


def test_preinventory_resume_does_not_replace_ambiguous_sdk_root_without_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _held_sdk_recovery_case(tmp_path, monkeypatch)
    operator_root = case["updater"]._load_workload_sdk_environment().WORKLOAD_OPERATOR_ROOT
    operator_root.mkdir()

    with pytest.raises(updates.UpdateError, match="without its durable prepare intent"):
        case["updater"]._resume_first_core_before_workload_inventory(
            case["stored"],
            case["transaction"],
            case["transaction_path"],
            lock_lease=object(),
            bootstrap_module=object(),
        )

    assert case["events"] == []


def test_invalid_existing_sdk_readback_still_reaches_authoritative_inventory_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid_sdk = {"installed": True, "componentId": updates.WORKLOAD_SDK_COMPONENT_ID}
    case = _held_sdk_recovery_case(tmp_path, monkeypatch, current_sdk=invalid_sdk)
    result, sdk_prepared = case["updater"]._resume_first_core_before_workload_inventory(
        case["stored"],
        case["transaction"],
        case["transaction_path"],
        lock_lease=object(),
        bootstrap_module=object(),
    )
    assert result is None
    assert sdk_prepared is False
    assert case["events"] == []

    policy_path = tmp_path / "authenticated-runtime-policy.json"
    policy_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(updates, "DEFAULT_PACKAGE_RUNTIME_POLICY", policy_path)
    updater = case["updater"]
    monkeypatch.setattr(
        updater,
        "_workload_plugin_owner_rows",
        lambda *_args: (case["policy"], [{"componentId": "cyrene-plugin-document-parsing"}]),
    )
    monkeypatch.setattr(updater, "_read_workload_package_runtime_receipt", lambda *_args: None)
    monkeypatch.setattr(
        updater, "_activity_catalog", lambda **_kwargs: ({"generation": 1, "sources": []}, [])
    )
    monkeypatch.setattr(updater, "_workload_source_principals", lambda *_args: {})
    monkeypatch.setattr(updater, "_load_workload_package_runtime", lambda: object())

    with pytest.raises(
        updates.UpdateError, match="operator SDK interpreter is not verified"
    ) as error:
        updater._read_workload_package_inventory("catalyst", ("cyrene-plugin-document-parsing",))
    assert error.value.code == "WORKLOAD_SDK_READBACK_REQUIRED"


def test_same_plan_recovery_precedes_ordinary_workload_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _workload_updates_updater(tmp_path)
    block = _public_first_core_block()
    events: list[str] = []
    transaction_root = tmp_path / "transactions"
    transaction_root.mkdir(mode=0o700)
    transaction_path = transaction_root / f"{block['planId']}.json"
    prior = {
        "transactionKind": "workload-assembly.v1",
        "action": "install",
        "planId": block["planId"],
        "planDigest": block["planDigest"],
        "workloadId": "catalyst",
        "phase": "applying",
        "maintenanceHolds": {},
    }
    updates._atomic_json(transaction_path, prior)
    stored = {
        "planId": block["planId"],
        "planDigest": block["planDigest"],
        "workloadId": "catalyst",
        "targetId": block["targetId"],
        "channel": "stable",
        "selections": {},
    }
    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    monkeypatch.setattr(updater, "_private_state_directory", lambda _name: transaction_root)
    monkeypatch.setattr(
        updater,
        "_resume_first_core_before_workload_inventory",
        lambda *_args, **_kwargs: events.append("held-core-and-sdk-resume") or ({}, True),
    )

    class InventoryReached(Exception):
        pass

    def build_workload_plan(*_args: Any, **_kwargs: Any) -> Any:
        events.append("ordinary-authoritative-inventory")
        raise InventoryReached

    monkeypatch.setattr(updater, "_build_workload_plan", build_workload_plan)

    with pytest.raises(InventoryReached):
        updater._apply_workload_install_assembled(
            stored,
            {"planId": block["planId"], "planDigest": block["planDigest"], "confirmed": True},
            lock_lease=object(),
            bootstrap_module=object(),
        )

    assert events == ["held-core-and-sdk-resume", "ordinary-authoritative-inventory"]


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
