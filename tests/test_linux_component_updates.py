"""Focused Linux component updater protocol and runtime integration tests."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
UPDATES_PATH = WORKSPACE_ROOT / "packaging" / "component_updates.py"
HELPER_PATH = WORKSPACE_ROOT / "packaging" / "cyrene-component-update-helper"
POLICY_PATH = WORKSPACE_ROOT / "packaging" / "org.cyrene.component-update.policy"
DEB_BUILDER_PATH = WORKSPACE_ROOT / "packaging" / "build-deb.sh"
spec = importlib.util.spec_from_file_location("cyrene_component_updates_test", UPDATES_PATH)
assert spec is not None and spec.loader is not None
updates = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = updates
spec.loader.exec_module(updates)


BROKER_BEGIN_NO_TOKEN_RESULTS = {
    "ACTIVE_TASKS": {
        "status": "ACTIVE_TASKS",
        "maintenance_token": None,
        "gate_generation": 7,
        "blocker_codes": ["ACTIVE_TASKS_PRESENT"],
    },
    "UNKNOWN": {
        "status": "UNKNOWN",
        "maintenance_token": None,
        "gate_generation": 7,
        "blocker_codes": ["ACTIVITY_SOURCE_UNKNOWN"],
    },
    "IDLE_RUNTIME_REQUIRES_UNLOAD": {
        "status": "IDLE_RUNTIME_REQUIRES_UNLOAD",
        "maintenance_token": None,
        "gate_generation": 7,
        "blocker_codes": ["IDLE_RUNTIME_REQUIRES_UNLOAD"],
    },
    "MAINTENANCE_ACTIVE": {
        "status": "MAINTENANCE_ACTIVE",
        "maintenance_token": None,
        "gate_generation": 7,
        "blocker_codes": ["MAINTENANCE_ALREADY_ACTIVE"],
    },
    # Sanitized BeginMaintenanceResult serialized by the real broker when the
    # expected gate generation changes between readiness and Begin.
    "STALE_READINESS": {
        "status": "STALE_READINESS",
        "maintenance_token": None,
        "gate_generation": 101,
        "blocker_codes": ["READINESS_GENERATION_STALE"],
    },
    "USER_CONFIRMATION_REQUIRED": {
        "status": "USER_CONFIRMATION_REQUIRED",
        "maintenance_token": None,
        "gate_generation": 7,
        "blocker_codes": ["USER_CONFIRMATION_REQUIRED"],
    },
}
BROKER_READINESS_STATUS_WIRE_VALUES = frozenset(
    {
        "READY",
        "ACTIVE_TASKS",
        "UNKNOWN",
        "IDLE_RUNTIME_REQUIRES_UNLOAD",
        "MAINTENANCE_ACTIVE",
        "STALE_READINESS",
        "USER_CONFIRMATION_REQUIRED",
    }
)


def _empty_updater(tmp_path: Path) -> updates.ComponentUpdater:
    catalog_path = tmp_path / "component-catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "generation": 1,
                "defaultChannel": "stable",
                "channels": {"stable": {}, "preview": {}},
                "components": [],
                "targets": [],
                "publishers": [],
            }
        ),
        encoding="utf-8",
    )
    return updates.ComponentUpdater(
        catalog_path=catalog_path,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "update-state",
        install_root=tmp_path / "install",
        broker_path=tmp_path / "missing-broker",
        trusted_catalog_digest=None,
    )


def test_workspace_bootstrap_uses_the_compiled_catalog_authority_pin(tmp_path: Path) -> None:
    updater = updates.ComponentUpdater(
        catalog_path=updates.DEFAULT_CATALOG,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "update-state",
        install_root=tmp_path / "install",
        release_lock_path=tmp_path / "missing-release-lock.json",
        broker_path=tmp_path / "missing-broker",
        load_active_catalog=False,
    )

    assert updater.bootstrap_catalog_digest == updates.TRUSTED_CATALOG_DIGEST
    assert updater.catalog_generation == 13


@pytest.mark.parametrize(
    ("profile_id", "version", "abi"),
    [
        ("linux-ubuntu-22.04-x86_64-python-3.12", "22.04", "glibc-2.35"),
        ("linux-ubuntu-24.04-x86_64-python-3.12", "24.04", "glibc-2.39"),
    ],
)
def test_product_python_target_selects_exact_host_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_id: str,
    version: str,
    abi: str,
) -> None:
    updater = updates.ComponentUpdater(
        catalog_path=updates.DEFAULT_CATALOG,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "update-state",
        install_root=tmp_path / "install",
        release_lock_path=WORKSPACE_ROOT / "release-lock.json",
        broker_path=tmp_path / "missing-broker",
        load_active_catalog=False,
    )
    lock = json.loads((WORKSPACE_ROOT / "release-lock.json").read_text(encoding="utf-8"))
    updater.native_python_profiles = lock["nativePythonProfiles"]
    monkeypatch.setattr(
        updates.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": version},
    )
    monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(updates.platform, "libc_ver", lambda: ("glibc", abi.removeprefix("glibc-")))
    component = updater.components["cyrene-navigator"]
    monkeypatch.setattr(updater, "_private_python_runtime_ready", lambda profile: False)
    assert updater._target_for(component) is None

    monkeypatch.setattr(updater, "_private_python_runtime_ready", lambda profile: True)
    target = updater._target_for(component)

    assert target is not None
    assert target["id"] == profile_id
    assert target["artifactKind"] == "python-bundle"


def test_portable_data_target_remains_independent_of_ubuntu_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = updates.ComponentUpdater(
        catalog_path=updates.DEFAULT_CATALOG,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "update-state",
        install_root=tmp_path / "install",
        release_lock_path=tmp_path / "missing-release-lock.json",
        broker_path=tmp_path / "missing-broker",
        load_active_catalog=False,
    )
    monkeypatch.setattr(
        updates.platform,
        "freedesktop_os_release",
        lambda: {"ID": "fedora", "VERSION_ID": "42"},
    )
    monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(updates.platform, "libc_ver", lambda: ("glibc", "2.39"))

    target = updater._target_for(updater.components["cyrene-product-contract-bundle"])

    assert target is not None
    assert target["id"] == "portable-contract-data-v1"
    assert target["artifactKind"] == "data-bundle"


def _configure_unmanaged_runtime_agent(
    updater: updates.ComponentUpdater,
    unit_dir: Path,
    *,
    distribution_version: str = "24.04",
    abi: str = "glibc-2.39",
) -> dict[str, object]:
    target_id = "linux-test-x86_64-systemd"
    component_id = "cy-runtime-agent"
    updater.targets[target_id] = {
        "target": {
            "os": "linux",
            "distribution": "ubuntu",
            "distributionVersion": distribution_version,
            "architecture": updates.platform.machine(),
            "abi": abi,
            "runtime": "systemd",
        }
    }
    updater.systemd_unit_dirs = (unit_dir,)
    component: dict[str, object] = {
        "componentId": component_id,
        "kind": "native-binary",
        "targets": [
            {
                "targetId": target_id,
                "artifactKind": "native-binary",
                "support": "supported",
            }
        ],
        "restart": {"group": "core-runtime", "unit": "cy-runtime-agent.service", "order": 41},
        "systemdUnit": "cy-runtime-agent.service",
    }
    updater.components[component_id] = component
    return component


@pytest.mark.parametrize(
    ("distribution_version", "abi"),
    [("22.04", "glibc-2.35"), ("24.04", "glibc-2.39")],
)
def test_os_supported_unmanaged_native_has_no_apply_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    distribution_version: str,
    abi: str,
) -> None:
    updater = _empty_updater(tmp_path)
    monkeypatch.setattr(
        updates.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": distribution_version},
    )
    monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        updates.platform,
        "libc_ver",
        lambda: ("glibc", abi.removeprefix("glibc-")),
    )
    component = _configure_unmanaged_runtime_agent(
        updater,
        tmp_path / "missing-units",
        distribution_version=distribution_version,
        abi=abi,
    )

    row = updater._result_component(
        component,
        {"status": "READY", "gate_generation": 1, "blocker_codes": []},
        phase="available",
        available_version="1.0.0",
        update_available=True,
    )

    assert row["supported"] is True
    assert row["target"]["runtime"] == "systemd"
    assert row["allowedActions"] == ["check", "stage"]
    assert row["blockers"][0]["code"] == "SERVICE_NOT_MANAGED"


def test_unmanaged_native_apply_refuses_before_begin_or_pointer_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _empty_updater(tmp_path)
    _configure_unmanaged_runtime_agent(updater, tmp_path / "missing-units")
    component_id = "cy-runtime-agent"
    plan_id = "plan-" + "a" * 32
    plan_digest = "sha256:" + "b" * 64
    artifact_digest = "sha256:" + "c" * 64
    release_root = updater.install_root / "components" / component_id
    releases = release_root / "releases"
    releases.mkdir(parents=True)
    old_pointer = "releases/0.9.0--" + "d" * 64
    (releases / old_pointer.removeprefix("releases/")).mkdir()
    active = release_root / "active"
    active.symlink_to(old_pointer)

    stage_directory = updater._private_state_directory("staged") / plan_id
    stage_directory.mkdir(mode=0o700)
    (stage_directory / "stage.json").write_text(
        json.dumps(
            {
                "phase": "staged",
                "channel": "stable",
                "plan": {"planId": plan_id, "planDigest": plan_digest},
                "components": [
                    {
                        "componentId": component_id,
                        "version": "1.0.0",
                        "manifestDigest": "sha256:" + "e" * 64,
                        "artifactDigest": artifact_digest,
                        "restartGroup": "core-runtime",
                        "manifest": {"version": "1.0.0"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(updater, "_validate_staged_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    monkeypatch.setattr(
        updater,
        "_readiness_for",
        lambda *args, **kwargs: pytest.fail(
            "unmanaged components must be rejected before readiness or Begin"
        ),
    )
    monkeypatch.setattr(
        updater,
        "_broker_request",
        lambda *args, **kwargs: pytest.fail("unmanaged components must not call BeginMaintenance"),
    )

    confirmation = {"planId": plan_id, "planDigest": plan_digest, "confirmed": True}
    with pytest.raises(updates.UpdateError) as error:
        updater.apply(plan_id, plan_digest, confirmation, channel="stable")

    assert error.value.code == "SERVICE_NOT_MANAGED"
    assert os.readlink(active) == old_pointer
    assert not (updater.state_root / "transactions" / f"{plan_id}.json").exists()


@pytest.mark.parametrize(
    ("version", "version_range", "expected"),
    [
        ("0.1.0", "=0.1.0", True),
        ("0.1.9", ">=0.1.0, <0.2.0", True),
        ("0.2.0", ">=0.1.0, <0.2.0", False),
    ],
)
def test_supported_dependency_ranges_are_exact_and_bounded(
    version: str, version_range: str, expected: bool
) -> None:
    assert updates._version_satisfies(version, version_range) is expected


@pytest.mark.parametrize(
    "version_range",
    ["^0.1.0", "~0.1", ">=0.1.0,<0.2.0", ">=0.2.0, <0.1.0", "latest"],
)
def test_unknown_or_malformed_dependency_ranges_fail_closed(version_range: str) -> None:
    with pytest.raises(updates.UpdateError, match="component versionRange"):
        updates._parse_supported_version_range(version_range)


def test_fixed_json_protocol_rejects_arbitrary_fields_and_extra_lines(tmp_path: Path) -> None:
    updater = _empty_updater(tmp_path)
    invalid = updater.handle(
        {
            "protocolVersion": updates.PROTOCOL_VERSION,
            "operation": "status",
            "executable": "/bin/sh",
        }
    )
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "INVALID_REQUEST"

    output = io.StringIO()
    updates.run_json_stdio(
        updater,
        io.StringIO(
            json.dumps({"protocolVersion": updates.PROTOCOL_VERSION, "operation": "status"})
            + "\n{}\n"
        ),
        output,
    )
    envelope = json.loads(output.getvalue())
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INVALID_REQUEST"


def test_empty_activity_catalog_is_unknown_instead_of_idle(tmp_path: Path) -> None:
    updater = _empty_updater(tmp_path)
    updater.activity_catalog_path.write_text(
        json.dumps({"schema_version": 1, "generation": 1, "sources": []}),
        encoding="utf-8",
    )

    with pytest.raises(updates.UpdateError) as error:
        updater._activity_catalog()

    assert error.value.code == "GATE_UNKNOWN"
    assert "will not assume the runtime is idle" in str(error.value)


def test_apply_confirmation_is_bound_to_plan_id_and_digest(tmp_path: Path) -> None:
    updater = _empty_updater(tmp_path)
    plan_id = "plan-" + "a" * 32
    plan_digest = "sha256:" + "b" * 64

    with pytest.raises(updates.UpdateError) as error:
        updater.apply(
            plan_id,
            plan_digest,
            {"planId": plan_id, "planDigest": plan_digest, "confirmed": False},
        )

    assert error.value.code == "CONFIRMATION_MISMATCH"


@pytest.mark.parametrize(
    ("distribution_version", "abi"),
    [("22.04", "glibc-2.35"), ("24.04", "glibc-2.39")],
)
def test_begin_payload_preserves_sha256_artifact_digest_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    distribution_version: str,
    abi: str,
) -> None:
    updater = _empty_updater(tmp_path)
    component_id = "cyrene-test"
    plan_id = "plan-" + "a" * 32
    plan_digest = "sha256:" + "b" * 64
    artifact_digest = "sha256:" + "c" * 64
    target_id = "linux-test-x86_64-systemd"
    updater.targets[target_id] = {
        "target": {
            "os": "linux",
            "distribution": "ubuntu",
            "distributionVersion": distribution_version,
            "architecture": "x86_64",
            "abi": abi,
            "runtime": "systemd",
        }
    }
    monkeypatch.setattr(
        updates.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": distribution_version},
    )
    monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        updates.platform,
        "libc_ver",
        lambda: ("glibc", abi.removeprefix("glibc-")),
    )
    unit_directory = tmp_path / "units"
    unit_directory.mkdir()
    (unit_directory / "cyrene-test.service").write_text(
        "[Service]\nExecStart=/usr/bin/cyrene component-run cyrene-test\n",
        encoding="utf-8",
    )
    (unit_directory / "cyrene-test.service").chmod(0o644)
    updater.systemd_unit_dirs = (unit_directory,)
    updater.components[component_id] = {
        "componentId": component_id,
        "kind": "native-binary",
        "targets": [
            {"targetId": target_id, "artifactKind": "native-binary", "support": "supported"}
        ],
        "restart": {"group": "single-service", "unit": "cyrene-test.service"},
        "systemdUnit": "cyrene-test.service",
    }
    stage_directory = updater._private_state_directory("staged") / plan_id
    stage_directory.mkdir(mode=0o700)
    stage_path = stage_directory / "stage.json"
    stage_path.write_text(
        json.dumps(
            {
                "phase": "staged",
                "channel": "stable",
                "plan": {"planId": plan_id, "planDigest": plan_digest},
                "components": [
                    {
                        "componentId": component_id,
                        "version": "1.0.0",
                        "manifestDigest": "sha256:" + "d" * 64,
                        "artifactDigest": artifact_digest,
                        "restartGroup": "single-service",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    monkeypatch.setattr(updater, "_validate_staged_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(updater, "_unit_exists", lambda component: True)
    monkeypatch.setattr(
        updater,
        "_readiness_for",
        lambda *args, **kwargs: {
            "status": "READY",
            "gate_generation": 4,
            "install_catalog_generation": 9,
            "active_task_count": 0,
            "active_tasks": [],
            "unknown_activity_sources": [],
            "active_worker_count": 0,
            "active_allocation_count": 0,
            "inflight_runtime_admission_count": 0,
            "requires_restart_confirmation": True,
        },
    )
    monkeypatch.setattr(
        updater,
        "_activity_catalog",
        lambda: ({"generation": 9, "sources": []}, ["cyrene-test"]),
    )
    monkeypatch.setattr(
        updater,
        "_capture_active_versions",
        lambda components: [{"componentId": component_id, "version": None}],
    )
    monkeypatch.setattr(updater, "_activate_transaction", lambda transaction: None)
    monkeypatch.setattr(updater, "_restart_transaction", lambda transaction: None)
    monkeypatch.setattr(updater, "_health_transaction", lambda transaction: None)
    monkeypatch.setattr(updater, "_applied_result", lambda transaction: {"status": "applied"})

    begin_payload: dict[str, object] = {}
    end_payload: dict[str, object] = {}
    begin_attempts = 0

    def broker_request(method: str, params: dict[str, object], *, request_id: str | None = None):
        nonlocal begin_attempts
        if method == "BeginMaintenance":
            begin_attempts += 1
            if begin_attempts == 1:
                return dict(BROKER_BEGIN_NO_TOKEN_RESULTS["STALE_READINESS"])
            begin_payload.update(params)
            assert params["request_id"] == request_id
            return {
                "status": "MAINTENANCE_ACTIVE",
                "maintenance_token": "t" * 32,
                "gate_generation": 8,
                "blocker_codes": [],
            }
        if method == "EndMaintenance":
            end_payload.update(params)
            end_payload["envelope_request_id"] = request_id
            return {"status": "SUCCESS"}
        raise AssertionError(f"unexpected broker method {method}")

    monkeypatch.setattr(updater, "_broker_request", broker_request)
    confirmation = {"planId": plan_id, "planDigest": plan_digest, "confirmed": True}
    transaction_path = updater.state_root / "transactions" / f"{plan_id}.json"
    with pytest.raises(
        updates.UpdateError, match="changed readiness before the maintenance gate"
    ) as error:
        updater.apply(plan_id, plan_digest, confirmation, channel="stable")

    assert error.value.code == "STALE_READINESS"
    assert error.value.maintenance_not_acquired is True
    assert not transaction_path.exists()
    assert stage_path.is_file()

    result = updater.apply(
        plan_id,
        plan_digest,
        confirmation,
        channel="stable",
    )

    assert result == {"status": "applied"}
    assert begin_attempts == 2
    assert begin_payload["component_artifact_digests"] == {component_id: artifact_digest}
    assert begin_payload["request_id"] == "cyrene-update-" + plan_id
    assert end_payload["request_id"] == begin_payload["request_id"]
    assert end_payload["target_kind"] == begin_payload["target_kind"]
    assert end_payload["envelope_request_id"] == f"cyrene-update-end-{plan_id}-success"


def test_begin_no_token_fixtures_cover_all_broker_refusal_statuses() -> None:
    assert updates.BEGIN_NO_TOKEN_STATUSES == BROKER_READINESS_STATUS_WIRE_VALUES - {"READY"}
    assert set(BROKER_BEGIN_NO_TOKEN_RESULTS) == updates.BEGIN_NO_TOKEN_STATUSES


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        ("ACTIVE_TASKS", "ACTIVE_TASKS"),
        ("UNKNOWN", "GATE_UNKNOWN"),
        ("IDLE_RUNTIME_REQUIRES_UNLOAD", "IDLE_RUNTIME_REQUIRES_UNLOAD"),
        ("MAINTENANCE_ACTIVE", "MAINTENANCE_ACTIVE"),
        ("STALE_READINESS", "STALE_READINESS"),
        ("USER_CONFIRMATION_REQUIRED", "USER_CONFIRMATION_REQUIRED"),
    ],
)
def test_begin_normal_no_token_status_is_classified_as_no_acquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected_code: str,
) -> None:
    updater = _empty_updater(tmp_path)
    transaction = {
        "planId": "plan-" + "a" * 32,
        "requestId": "cyrene-update-plan-" + "a" * 32,
        "planDigest": "sha256:" + "b" * 64,
        "componentArtifactDigests": {"cyrene-test": "sha256:" + "c" * 64},
        "phase": "begin_pending",
        "targetKind": "CORE_RUNTIME"
        if status == "IDLE_RUNTIME_REQUIRES_UNLOAD"
        else "PACKAGE_ONLY",
        "expectedCatalogGeneration": 9,
        "expectedActivitySources": ["cyrene-test"],
        "expectedGateGeneration": 7,
    }
    monkeypatch.setattr(
        updater,
        "_broker_request",
        lambda *args, **kwargs: dict(BROKER_BEGIN_NO_TOKEN_RESULTS[status]),
    )

    with pytest.raises(updates.UpdateError) as error:
        updater._begin_maintenance(transaction)

    assert error.value.code == expected_code
    assert error.value.maintenance_not_acquired is True


@pytest.mark.parametrize(
    "result",
    [
        {"status": "READY", "maintenance_token": None, "gate_generation": 7, "blocker_codes": []},
        {
            "status": "FUTURE_STATUS",
            "maintenance_token": None,
            "gate_generation": 7,
            "blocker_codes": [],
        },
        {
            "status": "STALE_READINESS",
            "gate_generation": 7,
            "blocker_codes": ["READINESS_GENERATION_STALE"],
        },
    ],
)
def test_begin_unknown_or_malformed_result_keeps_transaction_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, object],
) -> None:
    updater = _empty_updater(tmp_path)
    transaction = {
        "planId": "plan-" + "a" * 32,
        "requestId": "cyrene-update-plan-" + "a" * 32,
        "planDigest": "sha256:" + "b" * 64,
        "componentArtifactDigests": {"cyrene-test": "sha256:" + "c" * 64},
        "phase": "begin_pending",
        "targetKind": "PACKAGE_ONLY",
        "expectedCatalogGeneration": 9,
        "expectedActivitySources": ["cyrene-test"],
        "expectedGateGeneration": 7,
    }
    monkeypatch.setattr(updater, "_broker_request", lambda *args, **kwargs: dict(result))

    with pytest.raises(updates.UpdateError) as error:
        updater._begin_maintenance(transaction)

    assert error.value.code == "GATE_UNKNOWN"
    assert error.value.maintenance_not_acquired is False


def test_begin_invalid_argument_clears_unacquired_journal_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _empty_updater(tmp_path)
    plan_id = "plan-" + "a" * 32
    plan_digest = "sha256:" + "b" * 64
    transaction = {
        "planId": plan_id,
        "requestId": "cyrene-update-" + plan_id,
        "planDigest": plan_digest,
        "componentArtifactDigests": {"cyrene-test": "sha256:" + "c" * 64},
        "phase": "begin_pending",
        "targetKind": "PACKAGE_ONLY",
        "expectedCatalogGeneration": 9,
        "expectedActivitySources": ["cyrene-test"],
        "expectedGateGeneration": 4,
    }

    def refused_request(*args, **kwargs):
        raise updates.UpdateError("INVALID_ARGUMENT", "broker rejected malformed Begin parameters")

    monkeypatch.setattr(updater, "_broker_request", refused_request)
    with pytest.raises(updates.UpdateError) as error:
        updater._begin_maintenance(transaction)

    assert error.value.maintenance_not_acquired is True
    transaction_directory = updater._private_state_directory("transactions")
    transaction_path = transaction_directory / f"{plan_id}.json"
    updates._atomic_json(transaction_path, transaction)

    updater._clear_begin_pending(transaction, transaction_path)

    assert not transaction_path.exists()


@pytest.mark.parametrize("target_kind", ["PACKAGE_ONLY", "CORE_RUNTIME"])
def test_end_payload_carries_stable_request_id_and_target_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
) -> None:
    updater = _empty_updater(tmp_path)
    transaction = {
        "planId": "plan-" + "a" * 32,
        "requestId": "cyrene-update-" + "plan-" + "a" * 32,
        "targetKind": target_kind,
        "maintenanceToken": "t" * 32,
    }
    captured: dict[str, object] = {}

    def broker_request(method: str, params: dict[str, object], *, request_id: str | None = None):
        captured.update(method=method, params=params, envelope_request_id=request_id)
        return {"status": "SUCCESS"}

    monkeypatch.setattr(updater, "_broker_request", broker_request)

    updater._end_maintenance(transaction, outcome="SUCCESS", healthy=True)

    params = captured["params"]
    assert captured["method"] == "EndMaintenance"
    assert isinstance(params, dict)
    assert params["request_id"] == transaction["requestId"]
    assert captured["envelope_request_id"] == f"cyrene-update-end-{transaction['planId']}-success"
    assert params["target_kind"] == target_kind


def test_privileged_helper_has_a_fixed_root_only_command() -> None:
    helper = HELPER_PATH.read_text(encoding="utf-8")
    mode = stat.S_IMODE(HELPER_PATH.stat().st_mode)

    assert mode == 0o755
    assert 'if [ "$(/usr/bin/id -u)" -ne 0 ]' in helper
    assert "exec /usr/bin/cyrene update --json" in helper
    assert '"$1" = "placement-peer-plan"' in helper
    assert '"$1" = "placement-peer-receipt"' in helper
    assert 'exec /usr/bin/cyrene placement-peer-evidence "$@"' in helper
    assert helper.count('"$@"') == 1
    pinned_gh = helper.index("gh=/usr/libexec/cyrene-tools/gh")
    pinned_gh_hash_check = helper.index('/usr/bin/sha256sum "$gh"')
    peer_dispatch = helper.index('if [ "$mode" = "peer" ]')
    assert pinned_gh < pinned_gh_hash_check < peer_dispatch
    assert helper.index("PATH=/usr/libexec/cyrene-tools:") < peer_dispatch
    assert (
        subprocess.run(
            [str(HELPER_PATH), "--executable", "/bin/sh"],
            check=False,
            capture_output=True,
            text=True,
        ).returncode
        != 0
    )


def test_polkit_policy_requires_active_admin_authentication() -> None:
    policy = ET.parse(POLICY_PATH).getroot()
    action = policy.find("action")
    assert policy.tag == "policyconfig"
    assert action is not None
    assert action.attrib["id"] == "org.cyrene.component-update"
    defaults = action.find("defaults")
    assert defaults is not None
    assert defaults.findtext("allow_any") == "no"
    assert defaults.findtext("allow_inactive") == "no"
    assert defaults.findtext("allow_active") == "auth_admin_keep"
    annotations = {item.attrib.get("key"): item.text for item in action.findall("annotate")}
    assert annotations["org.freedesktop.policykit.exec.path"] == (
        "/usr/libexec/cyrene-component-update-helper"
    )


def test_deb_installs_root_helper_and_polkit_action() -> None:
    builder = DEB_BUILDER_PATH.read_text(encoding="utf-8")
    assert '"${STAGE_DIR}/usr/libexec"' in builder
    assert '"${STAGE_DIR}/usr/share/polkit-1/actions"' in builder
    assert "cyrene-component-update-helper" in builder
    assert "org.cyrene.component-update.policy" in builder
    assert "systemd, policykit-1" in builder


def test_deb_provisions_runtime_state_directory_fail_closed() -> None:
    builder = DEB_BUILDER_PATH.read_text(encoding="utf-8")

    assert "RUNTIME_STATE_DIR=/var/lib/cyrene/runtime" in builder
    assert (
        'install -d -o root -g cyrene-runtime-maintenance -m 2770 "$RUNTIME_STATE_DIR"' in builder
    )
    assert "stat -c '%u:%g:%a' -- \"$RUNTIME_STATE_DIR\"" in builder
    assert '"0:${AUTHORITY_GID}:2770"' in builder
    assert "Refusing to repair existing runtime state in place" in builder
    assert "chown -R root:cyrene-runtime-maintenance /var/lib/cyrene/runtime" not in builder


@pytest.mark.parametrize(
    ("component_id", "contract_variable"),
    [
        ("cy-workspace-web-bff", "CYRENE_WORKSPACE_WEB_BFF_PRODUCT_CONTRACT_ROOT_V2"),
        ("cy-workspace-connector", "CYRENE_WORKSPACE_CONNECTOR_PRODUCT_CONTRACT_ROOT_V2"),
    ],
)
def test_native_runner_pins_contract_root_to_active_release(
    component_id: str,
    contract_variable: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = tmp_path / "components" / component_id / "releases" / "1.0.0"
    binary = release / "bin" / "service"
    binary.parent.mkdir(parents=True)
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)
    contracts = release / "share" / "cyrene" / "product-contracts"
    contracts.mkdir(parents=True)
    active = release.parent.parent / "active"
    active.symlink_to("releases/1.0.0")
    manifest = {
        "componentId": component_id,
        "version": "1.0.0",
        "artifact": {
            "kind": "native-binary",
            "entrypoint": "bin/service",
            "arguments": ["--manifest-arg"],
        },
    }
    manifest["manifestDigest"] = updates._digest_json(manifest, "manifestDigest")
    manifest_path = release / "component-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    for directory in (
        tmp_path,
        tmp_path / "components",
        release.parent.parent,
        release.parent,
        release,
        release / "bin",
        release / "share",
        release / "share" / "cyrene",
        contracts,
    ):
        directory.chmod(0o755)
    manifest_path.chmod(0o644)

    real_lstat = Path.lstat

    def root_owned_active(path: Path):
        info = real_lstat(path)
        if path == active or path.is_relative_to(tmp_path):
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0)
        return info

    class ExecveReached(RuntimeError):
        pass

    seen: dict[str, object] = {}

    def capture_execve(path: str, argv: list[str], environment: dict[str, str]) -> None:
        seen.update(path=path, argv=argv, environment=environment)
        raise ExecveReached

    monkeypatch.setattr(Path, "lstat", root_owned_active)
    monkeypatch.setattr(updates.os, "execve", capture_execve)
    monkeypatch.setenv(contract_variable, "/caller/controlled/path")
    other_variable = next(
        value for value in updates.PRODUCT_CONTRACT_ROOT_ENV.values() if value != contract_variable
    )
    monkeypatch.setenv(other_variable, "/caller/controlled/other")

    with pytest.raises(ExecveReached):
        updates.run_component(
            component_id,
            install_root=tmp_path,
            startup_arguments=("--unit-arg", "value"),
        )

    assert seen["path"] == str(binary)
    assert seen["argv"] == [str(binary), "--manifest-arg", "--unit-arg", "value"]
    environment = seen["environment"]
    assert isinstance(environment, dict)
    assert environment[contract_variable] == str(contracts)
    assert other_variable not in environment


def test_native_runner_requires_service_user_to_read_public_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    component_id = "cy-workspace-web-bff"
    release = tmp_path / "components" / component_id / "releases" / "1.0.0"
    binary = release / "bin" / "service"
    binary.parent.mkdir(parents=True)
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)
    active = release.parent.parent / "active"
    active.symlink_to("releases/1.0.0")
    manifest = {
        "componentId": component_id,
        "version": "1.0.0",
        "artifact": {"kind": "native-binary", "entrypoint": "bin/service", "arguments": []},
    }
    manifest["manifestDigest"] = updates._digest_json(manifest, "manifestDigest")
    manifest_path = release / "component-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    for directory in (
        tmp_path,
        tmp_path / "components",
        release.parent.parent,
        release.parent,
        release,
        release / "bin",
    ):
        directory.chmod(0o755)
    manifest_path.chmod(0o644)

    real_lstat = Path.lstat

    def root_owned_active(path: Path):
        info = real_lstat(path)
        if path == active or path.is_relative_to(tmp_path):
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0)
        return info

    real_access = updates.os.access

    def service_user_access(path: Path, mode: int, **kwargs: object) -> bool:
        if path == manifest_path and mode == updates.os.R_OK:
            return False
        return real_access(path, mode, **kwargs)

    monkeypatch.setattr(Path, "lstat", root_owned_active)
    monkeypatch.setattr(updates.os, "access", service_user_access)
    monkeypatch.setattr(
        updates.os, "execve", lambda *args, **kwargs: pytest.fail("must reject before exec")
    )

    with pytest.raises(
        updates.UpdateError, match="manifest is not root-owned and readable"
    ) as error:
        updates.run_component(component_id, install_root=tmp_path)

    assert error.value.code == "INVALID_INSTALLED_RELEASE"


def test_manifest_v2_dual_read_support() -> None:
    manifest_v2 = {
        "schemaVersion": 2,
        "releaseId": "rel-v2",
        "componentId": "cy-workspace-relay",
        "version": "0.2.0",
        "channel": "stable",
        "target": {"os": "linux", "architecture": "x86_64"},
        "protocolVersion": "cyrene.workspace.authority.v1",
        "contentDigest": "sha256:" + "b" * 64,
        "artifact": {"kind": "native-binary"},
        "dependencies": [],
        "restart": {"group": "single-service", "unit": "cy-workspace-relay.service"},
        "source": {"repository": "DoHorizon-AI/Cyrene-Plugins-Official", "commit": "a" * 40},
        "provenance": {},
    }
    manifest_v2["manifestDigest"] = updates._digest_json(manifest_v2, "manifestDigest")
    assert manifest_v2["manifestDigest"].startswith("sha256:")
