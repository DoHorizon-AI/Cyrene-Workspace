"""Failure-closed fixtures for the first managed Core install path."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


updates = _load("native_core_bootstrap_updates_test", ROOT / "packaging" / "component_updates.py")
bootstrap = _load("native_core_bootstrap_test", ROOT / "packaging" / "native_core_bootstrap.py")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class FakeUpdater:
    """Record fixed updater operations without invoking a host service manager."""

    def __init__(self, root: Path, *, c10: bool = False) -> None:
        self.state_root = root / "state"
        self.install_root = root / "usr-lib-cyrene"
        # Match ComponentUpdater's normal three-entry lookup order with isolated paths.
        self.systemd_unit_dirs = (
            root / "etc-systemd",
            root / "lib-systemd",
            root / "usr-lib-systemd",
        )
        self.core_runtime_root = root / "var-lib-cyrene-runtime"
        self.core_run_root = root / "run-cyrene"
        component_ids = (
            bootstrap.C10_FIRST_CORE_COMPONENT_IDS if c10 else bootstrap.CORE_COMPONENT_IDS
        )
        self.components = {
            component_id: {
                "componentId": component_id,
                "systemdUnit": component_id + ".service",
                "restart": {
                    "group": (
                        "single-service"
                        if component_id in {"cyrene-runtime-maintenance", "cy-package-runtime"}
                        else "core-runtime"
                    ),
                    "order": order,
                    "unit": component_id + ".service",
                },
                **(
                    {
                        "compatibilityGroup": bootstrap.PACKAGE_RUNTIME_GROUP_ID,
                        "protocolVersion": bootstrap._PACKAGE_RUNTIME_PROTOCOLS[component_id],
                    }
                    if component_id in bootstrap._PACKAGE_RUNTIME_PROTOCOLS
                    else {}
                ),
            }
            for order, component_id in enumerate(component_ids, start=10)
        }
        self.catalog = {"compatibilityGroups": []}
        if c10:
            self.catalog["compatibilityGroups"] = [
                {
                    "groupId": bootstrap.PACKAGE_RUNTIME_GROUP_ID,
                    "groupVersion": "2",
                    "contractApiVersion": "0.1.0",
                    "wireApiVersion": "cyrene.runtime-maintenance.binding-operations.v1",
                    "contractLock": {
                        "repository": "DoHorizon-AI/Cyrene-Workspace",
                        "commit": "a" * 40,
                        "path": "governance/package-runtime-protocols-v1.lock.json",
                        "sha256": _digest(b"package runtime lock"),
                    },
                    "members": [
                        {
                            "componentId": component_id,
                            "requiredForAdoption": True,
                            "protocolVersion": protocol,
                        }
                        for component_id, protocol in bootstrap._PACKAGE_RUNTIME_PROTOCOLS.items()
                    ],
                }
            ]
        self.targets = {"target-ubuntu": {"id": "target-ubuntu", "target": "linux-ubuntu-test"}}
        self.catalog_generation = 4
        self.catalog_digest = _digest(b"catalog")
        self.core_bootstrap_eligible = True
        self.gate_generation = 9
        self.gpu_output = ""
        self.events: list[Any] = []
        self.pointers: dict[str, str | None] = dict.fromkeys(component_ids)
        self.unit_pids: dict[str, str] = {}
        self.bootstrap_broker_identity = None
        self.initial_broker_manifest = None
        if c10:
            old_digest = _digest(b"initial broker manifest")
            group = self.catalog["compatibilityGroups"][0]
            self.initial_broker_manifest = {
                "schemaVersion": 2,
                "componentId": "cyrene-runtime-maintenance",
                "version": "0.9.0",
                "manifestDigest": old_digest,
                "artifact": {"digest": _digest(b"initial broker artifact")},
                "protocolVersion": bootstrap._PACKAGE_RUNTIME_PROTOCOLS[
                    "cyrene-runtime-maintenance"
                ],
                "compatibility": {
                    "groupId": bootstrap.PACKAGE_RUNTIME_GROUP_ID,
                    "groupVersion": group["groupVersion"],
                    "contractApiVersion": group["contractApiVersion"],
                    "wireApiVersion": group["wireApiVersion"],
                    "contractLock": group["contractLock"],
                },
            }
            self.pointers["cyrene-runtime-maintenance"] = "0.9.0--" + old_digest.removeprefix(
                "sha256:"
            )
            self.bootstrap_broker_identity = {
                "componentId": "cyrene-runtime-maintenance",
                "pointerIdentity": self.pointers["cyrene-runtime-maintenance"],
                "version": "0.9.0",
                "manifestDigest": old_digest,
                "artifactDigest": _digest(b"initial broker artifact"),
                "systemdUnit": "cyrene-runtime-maintenance.service",
                "mainPid": "42",
                "executable": "/usr/lib/cyrene/fake-initial-broker",
            }
            self.unit_pids["cyrene-runtime-maintenance.service"] = "42"
        self.gate_counts = {
            "status": "MAINTENANCE_ACTIVE",
            "blocker_codes": [],
            "active_task_count": 0,
            "active_tasks": [],
            "inflight_runtime_admission_count": 0,
            "active_worker_count": 0,
            "active_allocation_count": 0,
        }
        self.main_pid = "0"
        self.stopped_units: set[str] = set()

    def _reload_catalog_for_operation(self) -> None:
        pass

    def _require_authorized_process(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {"components": []}

    @contextmanager
    def _exclusive_update_lock(self):
        yield

    def _ensure_state_root(self) -> Path:
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self.state_root

    @staticmethod
    def _atomic_json_file(path: Path, value: dict[str, Any], *, mode: int) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        path.chmod(mode)

    def _active_native_pointer_identity(self, component_id: str) -> str | None:
        return self.pointers[component_id]

    def _installed(self, component: dict[str, Any]) -> dict[str, Any]:
        assert component["componentId"] == "cyrene-runtime-maintenance"
        identity = self.bootstrap_broker_identity
        assert identity is not None
        return {
            "active": True,
            "identityAttested": True,
            "manifest": self.initial_broker_manifest,
            "pointerIdentity": identity["pointerIdentity"],
            "manifestDigest": identity["manifestDigest"],
            "artifactDigest": identity["artifactDigest"],
        }

    def _read_active_receipt(self, component_id: str) -> dict[str, Any]:
        assert component_id == "cyrene-runtime-maintenance"
        identity = self.bootstrap_broker_identity
        assert identity is not None
        return {
            "manifest": self.initial_broker_manifest,
            "manifestDigest": identity["manifestDigest"],
            "artifactDigest": identity["artifactDigest"],
        }

    def _target_for(self, component: dict[str, Any]) -> dict[str, Any]:
        return self.targets["target-ubuntu"]

    def _broker_request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        self.events.append(("broker", method, params))
        if method == "Health":
            response = {
                "status": "SERVING",
                "catalog_generation": 1,
                "gate_generation": self.gate_generation,
                "core_bootstrap_eligible": self.core_bootstrap_eligible,
            }
            if self.catalog["compatibilityGroups"]:
                response.update(
                    {
                        "protocol_version": "cyrene.runtime-maintenance.broker.v1",
                        "capabilities": ["cyrene.runtime-maintenance.state.v2"],
                    }
                )
            return response
        if method == "BeginCoreBootstrap":
            return {
                "status": "MAINTENANCE_ACTIVE",
                "maintenance_origin": "CORE_BOOTSTRAP",
                "readiness_claimed": False,
                "held": True,
                "maintenance_token": "t" * 40,
                "gate_generation": 10,
                "blocker_codes": [],
            }
        if method == "EndMaintenance":
            self.events.append(
                ("end", params["outcome"], params["healthy"], params["maintenance_token"])
            )
            return {"status": "READY", "unlocked": True}
        raise AssertionError(method)

    def _activity_catalog(self) -> tuple[dict[str, Any], list[str]]:
        return {"generation": 1}, ["source-a", "source-b"]

    def _load_native_package_runtime_bootstrap(self) -> Any:
        updater = self

        class RuntimeProbe:
            def probe_runtime_authority(
                self, _catalog: dict[str, Any], *, expected_catalog_generation: int
            ) -> dict[str, Any]:
                updater.events.append(("package-runtime-authority", expected_catalog_generation))
                return {
                    "authority": "platform_package_runtime",
                    "protocol_version": "cy-package-runtime.control.v1",
                    "catalog_generation": expected_catalog_generation,
                    "capabilities": ["cy-package-runtime.binding-operation-admission.v1"],
                }

        return RuntimeProbe()

    def check(
        self, component_ids: list[str], *, channel: Any = None, include_readiness: bool = True
    ) -> dict[str, Any]:
        if self.catalog["compatibilityGroups"] and "cyrene-kernel" in component_ids:
            component_ids = list(bootstrap.C10_FIRST_CORE_COMPONENT_IDS)
        components = [
            {
                "componentId": component_id,
                "version": "1.0.0",
                "manifestDigest": _digest(component_id.encode()),
                "artifactDigest": _digest((component_id + " artifact").encode()),
                "restartGroup": "core-runtime",
                **(
                    {"protocolVersion": bootstrap._PACKAGE_RUNTIME_PROTOCOLS[component_id]}
                    if component_id in bootstrap._PACKAGE_RUNTIME_PROTOCOLS
                    else {}
                ),
            }
            for component_id in component_ids
        ]
        material = {
            "schemaVersion": 1,
            "channel": channel or "stable",
            "catalogGeneration": self.catalog_generation,
            "catalogDigest": self.catalog_digest,
            "components": components,
        }
        digest = _digest(updates.canonical_jcs(material))
        plan_id = "plan-" + digest.split(":", 1)[1][:32]
        plan = {**material, "planId": plan_id, "planDigest": digest, "phase": "checked"}
        path = self.state_root / "plans" / (plan_id + ".json")
        self._atomic_json_file(path, plan, mode=0o600)
        return {"plan": plan, "plans": [plan], "components": []}

    def _validate_plan_identity(self, plan_id: Any, plan_digest: Any) -> None:
        assert isinstance(plan_id, str) and plan_id.startswith("plan-")
        assert isinstance(plan_digest, str) and plan_digest.startswith("sha256:")

    def _resolve_channel(self, channel: Any) -> str:
        return channel or "stable"

    def stage(self, plan_id: str, plan_digest: str, *, channel: Any = None) -> dict[str, Any]:
        self.events.append(("stage", plan_id))
        plan = json.loads((self.state_root / "plans" / (plan_id + ".json")).read_text())
        components = []
        for item in plan["components"]:
            component_id = item["componentId"]
            unit = self.components[component_id]["systemdUnit"].encode()
            binary_path = "bin/" + component_id
            unit_path = "systemd/" + self.components[component_id]["systemdUnit"]
            release_name = item["version"] + "--" + item["manifestDigest"].removeprefix("sha256:")
            release = self.install_root / "components" / component_id / "releases" / release_name
            (release / "systemd").mkdir(mode=0o755, parents=True, exist_ok=True)
            unit_file = release / "systemd" / unit_path.removeprefix("systemd/")
            unit_file.write_bytes(unit)
            unit_file.chmod(0o644)
            binary_file = release / binary_path
            binary_file.parent.mkdir(parents=True, exist_ok=True)
            binary_file.write_bytes(b"signed test executable")
            binary_file.chmod(0o755)
            manifest = {
                "componentId": component_id,
                "version": item["version"],
                "manifestDigest": item["manifestDigest"],
                "schemaVersion": (
                    2
                    if self.catalog["compatibilityGroups"]
                    and component_id in bootstrap._PACKAGE_RUNTIME_PROTOCOLS
                    else 1
                ),
                "protocolVersion": self.components[component_id].get("protocolVersion"),
                "compatibility": (
                    {
                        "groupId": bootstrap.PACKAGE_RUNTIME_GROUP_ID,
                        "groupVersion": "2",
                        "contractApiVersion": "0.1.0",
                        "wireApiVersion": "cyrene.runtime-maintenance.binding-operations.v1",
                        "contractLock": self.catalog["compatibilityGroups"][0]["contractLock"],
                    }
                    if self.catalog["compatibilityGroups"]
                    and component_id in bootstrap._PACKAGE_RUNTIME_PROTOCOLS
                    else None
                ),
                "artifact": {
                    "entrypoint": binary_path,
                    "files": {
                        binary_path: _digest(binary_file.read_bytes()),
                        unit_path: _digest((release / unit_path).read_bytes()),
                    },
                },
                "health": None,
            }
            components.append({**item, "bundleIdentity": None, "manifest": manifest})
        record = {
            "schemaVersion": 2,
            "phase": "staged",
            "channel": plan["channel"],
            "plan": plan,
            "components": components,
        }
        path = self.state_root / "staged" / plan_id / "stage.json"
        self._atomic_json_file(path, record, mode=0o600)
        return {"components": components}

    def _validate_staged_record(
        self, record: dict[str, Any], *, expected_plan_id: str, expected_plan_digest: str
    ) -> None:
        assert record["plan"]["planId"] == expected_plan_id
        assert record["plan"]["planDigest"] == expected_plan_digest

    def _unit_uses_component_runner(self, _unit: Path, _component_id: str) -> bool:
        return True

    def _capture_active_versions(self, components: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "componentId": item["componentId"],
                "pointerIdentity": self.pointers[item["componentId"]],
                "identityAttested": (
                    item["componentId"] == "cyrene-runtime-maintenance"
                    and self.bootstrap_broker_identity is not None
                ),
                **(
                    {
                        "releaseIdentity": self.bootstrap_broker_identity["manifestDigest"],
                        "manifestDigest": self.bootstrap_broker_identity["manifestDigest"],
                        "artifactDigest": self.bootstrap_broker_identity["artifactDigest"],
                        "version": self.bootstrap_broker_identity["version"],
                        "bundleIdentity": None,
                    }
                    if item["componentId"] == "cyrene-runtime-maintenance"
                    and self.bootstrap_broker_identity is not None
                    else {}
                ),
            }
            for item in components
        ]

    def _activate_transaction(self, transaction: dict[str, Any]) -> None:
        self.events.append(
            ("activate", [item["componentId"] for item in transaction["components"]])
        )
        for item in transaction["components"]:
            self.pointers[item["componentId"]] = (
                item["version"] + "--" + item["manifestDigest"].removeprefix("sha256:")
            )

    def _restart_transaction(self, transaction: dict[str, Any]) -> None:
        self.events.append(("restart", [item["componentId"] for item in transaction["components"]]))

    def _run_systemctl(self, operation: str, unit: str) -> None:
        self.events.append((operation, unit))
        if operation == "stop":
            self.stopped_units.add(unit)

    def _wait_unit_active(self, unit: str) -> None:
        self.events.append(("active", unit))

    def _health_transaction(self, _transaction: dict[str, Any]) -> None:
        self.events.append(("health",))

    def _fsync_directory(self, _path: Path) -> None:
        pass

    def runner(self, *_args: Any, **_kwargs: Any) -> Any:
        command = _args[0] if _args else []
        stdout = self.gpu_output
        if command[:2] == ["systemctl", "show"] and len(command) > 2:
            unit = command[-1]
            property_arg = command[2]
            if property_arg == "--property=ActiveState,SubState,MainPID,ControlPID":
                pid = self.unit_pids.get(unit, "0")
                active = "active" if pid != "0" and unit not in self.stopped_units else "inactive"
                substate = "running" if active == "active" else "dead"
                stdout = f"{active}\n{substate}\n{pid if active == 'active' else '0'}\n0"
            elif property_arg.startswith("--property=FragmentPath"):
                stdout = str(self.systemd_unit_dirs[0] / unit)
            elif property_arg.startswith("--property=DropInPaths"):
                stdout = ""
            elif property_arg.startswith("--property=NeedDaemonReload"):
                stdout = "no"
        if command[:2] == ["systemctl", "stop"]:
            unit = command[2]
            self.events.append(("stop", unit))
            self.stopped_units.add(unit)
        if command[:2] == ["systemctl", "daemon-reload"]:
            self.events.append(("daemon-reload",))
        if command and command[:2] == ["systemctl", "show"]:
            unit = command[-1]
            if len(command) > 2 and command[2] == "--property=ActiveState":
                stdout = "inactive" if unit in self.stopped_units else "active"
            elif len(command) > 2 and command[2] == "--property=MainPID":
                stdout = (
                    "0" if unit in self.stopped_units else self.unit_pids.get(unit, self.main_pid)
                )
        return type("Completed", (), {"returncode": 0, "stdout": stdout})()

    def _readiness_for(
        self, _target_kind: str, *, requires_restart: bool, force: bool = False
    ) -> dict[str, Any]:
        self.events.append(("readiness",))
        return dict(self.gate_counts)

    def _end_maintenance(self, transaction: dict[str, Any], *, outcome: str, healthy: bool) -> None:
        self.events.append(("end", outcome, healthy, transaction.get("maintenanceToken")))

    def _activate_native(
        self, component_id: str, pointer_identity: str | None, *, expected_current: str | None
    ) -> None:
        assert self.pointers[component_id] == expected_current
        self.pointers[component_id] = pointer_identity

    def _clear_active_receipt(self, _component_id: str) -> None:
        pass

    def _write_active_receipt(self, _item: dict[str, Any]) -> None:
        pass


class FakeFirstProducts:
    """Model receipt-bound Product hooks without starting real Product services."""

    def __init__(self, updater: FakeUpdater) -> None:
        self.updater = updater
        self.products = [
            {
                "service": service,
                "componentId": "cyrene-" + service,
                "version": "1.0.0",
                "manifestDigest": _digest((service + "-manifest").encode()),
                "artifactDigest": _digest((service + "-artifact").encode()),
                "sourceCommit": "a" * 40,
                "targetProfileId": "ubuntu-24.04-x86_64",
            }
            for service in ("catalyst", "exchange", "navigator", "reactor", "yield")
        ]
        self.identity = {
            "schemaVersion": 1,
            "receiptDigest": _digest(b"verified-first-product-receipt"),
            "installer": {
                "debSha256": _digest(b"installer"),
                "sourceCommit": "b" * 40,
                "targetId": "ubuntu-24.04-x86_64",
            },
            "products": self.products,
        }
        self.post_end_results = ["pending", "complete"]
        self.make_runtime_unknown_after_activate = False

    def check(self, _updater: Any, _core_plan: dict[str, Any]) -> dict[str, Any]:
        self.updater.events.append(("products-check",))
        return self.identity

    def stage(
        self, _updater: Any, _core_plan: dict[str, Any], identity: dict[str, Any]
    ) -> dict[str, Any]:
        self.updater.events.append(("products-stage",))
        assert identity == self.identity
        return {
            "status": "staged",
            "receiptDigest": identity["receiptDigest"],
            "products": self.products,
        }

    def activate_held(
        self, _updater: Any, _plan: dict[str, Any], transaction: dict[str, Any]
    ) -> dict[str, Any]:
        self.updater.events.append(("products-activate-held",))
        transaction["firstProducts"]["phase"] = "active"
        if self.make_runtime_unknown_after_activate:
            self.updater.gate_counts["active_worker_count"] = None
        return {"status": "active"}

    def verify_held(
        self, _updater: Any, _plan: dict[str, Any], _transaction: dict[str, Any]
    ) -> None:
        self.updater.events.append(("products-verify-held",))

    def rollback_pre_end(
        self, _updater: Any, _plan: dict[str, Any], transaction: dict[str, Any]
    ) -> None:
        self.updater.events.append(("products-rollback-pre-end",))
        transaction["firstProducts"]["phase"] = "staged"

    def complete_post_end(
        self, _updater: Any, _plan: dict[str, Any], transaction: dict[str, Any]
    ) -> dict[str, Any]:
        self.updater.events.append(("products-complete-post-end",))
        status = self.post_end_results.pop(0)
        transaction["firstProducts"]["phase"] = (
            "complete" if status == "complete" else "post_end_pending"
        )
        return {"status": status, "phase": transaction["firstProducts"]["phase"]}

    def recover_post_end(
        self, _updater: Any, _plan: dict[str, Any], transaction: dict[str, Any]
    ) -> dict[str, Any]:
        self.updater.events.append(("products-recover-post-end",))
        status = self.post_end_results.pop(0)
        transaction["firstProducts"]["phase"] = (
            "complete" if status == "complete" else "post_end_pending"
        )
        return {"status": status, "phase": transaction["firstProducts"]["phase"]}


def _fake_proc(
    root: Path,
    *,
    core: str | None = None,
    malformed: bool = False,
    empty_command: bool = False,
    missing_executable: bool = False,
) -> Path:
    root.mkdir()
    process = root / "100"
    process.mkdir()
    if malformed:
        (process / "stat").write_text("bad")
        return root
    (process / "stat").write_text("100 (process) S 1")
    command = b"" if empty_command else (core or "python").encode() + b"\0"
    (process / "cmdline").write_bytes(command)
    executable = root.parent / (core or "sleep")
    executable.touch()
    if not missing_executable:
        try:
            (process / "exe").symlink_to(executable)
        except FileExistsError:
            pass
    return root


def _check_plan(updater: FakeUpdater, tmp_path: Path) -> dict[str, Any]:
    proc = _fake_proc(tmp_path / "proc")
    return bootstrap.check(updater, proc_root=proc)["plan"]


def _product_confirmation(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": bootstrap.CORE_BOOTSTRAP_MODE,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
        "includeProducts": True,
        "firstProducts": plan["firstProducts"],
    }


def test_unknown_legacy_or_incomplete_process_inventory_refuses_check(tmp_path: Path) -> None:
    updater = FakeUpdater(tmp_path)
    legacy = _fake_proc(tmp_path / "proc-legacy", core="cyrene-kernel")
    with pytest.raises(ValueError, match="Legacy or manually started"):
        bootstrap.check(updater, proc_root=legacy)
    unknown = _fake_proc(tmp_path / "proc-unknown", malformed=True)
    with pytest.raises(RuntimeError, match="Process inventory"):
        bootstrap.check(updater, proc_root=unknown)


def test_empty_kernel_thread_cmdline_is_not_an_unknown_user_process(tmp_path: Path) -> None:
    kernel_thread = _fake_proc(
        tmp_path / "proc-kthread", empty_command=True, missing_executable=True
    )
    assert bootstrap._core_process_snapshot(kernel_thread) == []

    unreadable_user_exe = _fake_proc(tmp_path / "proc-no-exe", missing_executable=True)
    with pytest.raises(RuntimeError, match="Process executable is unavailable"):
        bootstrap._core_process_snapshot(unreadable_user_exe)


def test_deleted_non_core_executable_is_read_from_proc_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc_root = _fake_proc(tmp_path / "proc-deleted-user")
    exe_link = proc_root / "100" / "exe"
    executable = Path(os.readlink(exe_link))
    descriptor = os.open(executable, os.O_RDONLY)
    executable.unlink()
    original_readlink = os.readlink
    original_stat = os.stat

    def deleted_proc_readlink(path: Any, *args: Any, **kwargs: Any) -> str:
        if Path(path) == exe_link:
            return f"{executable} (deleted)"
        return original_readlink(path, *args, **kwargs)

    def deleted_proc_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == exe_link:
            return os.fstat(descriptor)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", deleted_proc_readlink)
    monkeypatch.setattr(os, "stat", deleted_proc_stat)
    try:
        assert bootstrap._core_process_snapshot(proc_root) == []
    finally:
        os.close(descriptor)


def test_deleted_core_executable_still_blocks_first_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    proc_root = _fake_proc(tmp_path / "proc-deleted-core", core="cyrene-kernel")
    process = proc_root / "100"
    (process / "cmdline").write_bytes(b"python\0")
    exe_link = process / "exe"
    executable = Path(os.readlink(exe_link))
    descriptor = os.open(executable, os.O_RDONLY)
    executable.unlink()
    original_readlink = os.readlink
    original_stat = os.stat

    def deleted_proc_readlink(path: Any, *args: Any, **kwargs: Any) -> str:
        if Path(path) == exe_link:
            return f"{executable} (deleted)"
        return original_readlink(path, *args, **kwargs)

    def deleted_proc_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == exe_link:
            return os.fstat(descriptor)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", deleted_proc_readlink)
    monkeypatch.setattr(os, "stat", deleted_proc_stat)
    try:
        with pytest.raises(ValueError, match="Legacy or manually started"):
            bootstrap.check(updater, proc_root=proc_root)
    finally:
        os.close(descriptor)


def test_unreadable_process_executable_keeps_core_inventory_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc_root = _fake_proc(tmp_path / "proc-unreadable-executable")
    exe_link = proc_root / "100" / "exe"
    original_stat = os.stat

    def deny_process_executable(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == exe_link:
            raise PermissionError("mock unreadable procfs executable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", deny_process_executable)
    with pytest.raises(RuntimeError, match="Process executable is unreadable"):
        bootstrap._core_process_snapshot(proc_root)


def test_non_regular_process_executable_keeps_core_inventory_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc_root = _fake_proc(tmp_path / "proc-non-regular-executable")
    exe_link = proc_root / "100" / "exe"
    original_stat = os.stat

    def report_directory_for_executable(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == exe_link:
            return original_stat(tmp_path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", report_directory_for_executable)
    with pytest.raises(RuntimeError, match="Process executable is unreadable"):
        bootstrap._core_process_snapshot(proc_root)


@pytest.mark.parametrize("resource", ["kernel-journal", "worker-transport"])
def test_historical_kernel_or_worker_ownership_blocks_fresh_check(
    tmp_path: Path, resource: str
) -> None:
    updater = FakeUpdater(tmp_path)
    if resource == "kernel-journal":
        updater.core_runtime_root.mkdir(parents=True)
        (updater.core_runtime_root / "journal.jsonl").write_text('{"event":"KernelStarted"}\n')
    else:
        (updater.core_run_root / "workers").mkdir(parents=True)
    with pytest.raises(
        ValueError, match="Existing (Kernel runtime ownership journal|Core runtime ownership path)"
    ):
        bootstrap.check(updater, proc_root=_fake_proc(tmp_path / "proc-fresh"))


def test_c9_fresh_inventory_keeps_maintenance_broker_outside_legacy_core_set(
    tmp_path: Path,
) -> None:
    updater = FakeUpdater(tmp_path)
    proc_root = _fake_proc(tmp_path / "proc-c9-broker", core="cyrene-runtime-maintenance")
    bootstrap._assert_fresh(updater, proc_root=proc_root)


def test_stage_is_allowed_without_process_idle_proof(tmp_path: Path) -> None:
    updater = FakeUpdater(tmp_path)
    plan = _check_plan(updater, tmp_path)
    # Staging verifies and stores bytes without evaluating live process state.
    updater.core_bootstrap_eligible = False
    updater.gate_generation = 10
    updater.gate_counts["status"] = "UNKNOWN"
    result = bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    assert result["status"] == "staged"
    assert len(result["components"]) == 4


def test_c10_first_core_checks_stages_and_applies_exact_group_in_dependency_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path, c10=True)
    monkeypatch.setattr(
        bootstrap,
        "_verified_running_c10_broker",
        lambda *_args: updater.bootstrap_broker_identity,
    )
    monkeypatch.setattr(bootstrap, "_stop_initial_c10_broker", lambda *_args: None)
    plan = bootstrap.check(updater, proc_root=_fake_proc(tmp_path / "proc-c10"))["plan"]
    assert {item["componentId"] for item in plan["components"]} == set(
        bootstrap.C10_FIRST_CORE_COMPONENT_IDS
    )
    staged = bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    assert len(staged["components"]) == 6

    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    monkeypatch.setattr(bootstrap, "_verify_started_processes", lambda *_args: None)
    confirmation = {
        "mode": bootstrap.CORE_BOOTSTRAP_MODE,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
        "bootstrapBroker": plan["bootstrapBroker"],
    }
    result = bootstrap.apply(
        updater,
        plan["planId"],
        plan["planDigest"],
        confirmation,
        proc_root=_fake_proc(tmp_path / "proc-c10-apply"),
    )

    starts = [event[1] for event in updater.events if event[0] == "start"]
    expected = [
        updater.components[item]["systemdUnit"] for item in bootstrap.C10_FIRST_CORE_COMPONENT_IDS
    ]
    assert starts == expected
    assert result["status"] == "installed"
    assert result["components"] == list(bootstrap.C10_FIRST_CORE_COMPONENT_IDS)
    assert set(updater.pointers) == set(bootstrap.C10_FIRST_CORE_COMPONENT_IDS)
    assert all(pointer is not None for pointer in updater.pointers.values())
    readiness = next(
        index
        for index, event in enumerate(updater.events)
        if event[0] == "package-runtime-authority"
    )
    end = next(index for index, event in enumerate(updater.events) if event[0] == "end")
    assert readiness < end


@pytest.mark.parametrize("defect", ["missing-member", "mixed-member"])
def test_c10_first_core_rejects_incomplete_or_mixed_group_before_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    updater = FakeUpdater(tmp_path, c10=True)
    monkeypatch.setattr(
        bootstrap,
        "_verified_running_c10_broker",
        lambda *_args: updater.bootstrap_broker_identity,
    )
    original_check = updater.check

    def incomplete_check(component_ids: list[str], **kwargs: Any) -> dict[str, Any]:
        result = original_check(component_ids, **kwargs)
        items = result["plan"]["components"]
        if defect == "missing-member":
            result["plan"]["components"] = [
                item for item in items if item["componentId"] != "cy-package-runtime"
            ]
        else:
            result["plan"]["components"] = [
                item for item in items if item["componentId"] != "cy-package-runtime"
            ] + [
                {
                    "componentId": "cyrene-untrusted-helper",
                    "version": "1.0.0",
                    "manifestDigest": _digest(b"untrusted manifest"),
                    "artifactDigest": _digest(b"untrusted artifact"),
                }
            ]
        return result

    monkeypatch.setattr(updater, "check", incomplete_check)
    with pytest.raises(ValueError, match="exact supported C9 or C10 cohort"):
        bootstrap.check(updater, proc_root=_fake_proc(tmp_path / f"proc-c10-{defect}"))


def test_c10_current_broker_receipt_rejects_schema_one_and_unpinned_identity(
    tmp_path: Path,
) -> None:
    updater = FakeUpdater(tmp_path, c10=True)
    group = updater.catalog["compatibilityGroups"][0]
    component_id = "cyrene-runtime-maintenance"
    manifest = {
        "schemaVersion": 2,
        "componentId": component_id,
        "version": "0.9.0",
        "manifestDigest": _digest(b"initial broker manifest"),
        "protocolVersion": bootstrap._PACKAGE_RUNTIME_PROTOCOLS[component_id],
        "compatibility": {
            "groupId": bootstrap.PACKAGE_RUNTIME_GROUP_ID,
            "groupVersion": group["groupVersion"],
            "contractApiVersion": group["contractApiVersion"],
            "wireApiVersion": group["wireApiVersion"],
            "contractLock": group["contractLock"],
        },
    }
    installed = {
        "active": True,
        "identityAttested": True,
        "manifest": manifest,
        "pointerIdentity": "0.9.0--" + manifest["manifestDigest"].removeprefix("sha256:"),
        "manifestDigest": manifest["manifestDigest"],
        "artifactDigest": _digest(b"initial broker artifact"),
    }
    assert bootstrap._validate_c10_broker_manifest(updater, installed) is manifest

    for field, value in (
        ("schemaVersion", 1),
        ("protocolVersion", "cyrene.runtime-maintenance.broker.v0"),
    ):
        changed = {**manifest, field: value}
        with pytest.raises(ValueError, match="exact C10 contract"):
            bootstrap._validate_c10_broker_manifest(updater, {**installed, "manifest": changed})

    with pytest.raises(ValueError, match="active pointer"):
        bootstrap._validate_c10_broker_manifest(
            updater, {**installed, "pointerIdentity": "different-release"}
        )


def _install_c10_broker_fixture(updater: FakeUpdater, tmp_path: Path) -> tuple[Path, Path]:
    """Create the exact signed-release, active-unit, and live-PID evidence used by C10."""

    component_id = "cyrene-runtime-maintenance"
    identity = updater.bootstrap_broker_identity
    assert identity is not None
    executable = (
        updater.install_root
        / "components"
        / component_id
        / "releases"
        / identity["pointerIdentity"]
        / "bin"
        / component_id
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"signed broker executable")
    executable.chmod(0o755)
    updater.bootstrap_broker_identity["executable"] = str(executable)
    manifest = updater.initial_broker_manifest
    assert manifest is not None
    manifest["artifact"] = {
        "entrypoint": "bin/" + component_id,
        "files": {"bin/" + component_id: _digest(executable.read_bytes())},
    }
    unit_name = updater.components[component_id]["systemdUnit"]
    unit_bytes = (
        "[Service]\nExecStart=/usr/bin/cyrene component-run " + component_id + " --\n"
    ).encode()
    source_unit = executable.parent.parent / "systemd" / unit_name
    source_unit.parent.mkdir(parents=True)
    source_unit.write_bytes(unit_bytes)
    source_unit.chmod(0o644)
    installed_unit = updater.systemd_unit_dirs[0] / unit_name
    installed_unit.parent.mkdir(parents=True)
    installed_unit.write_bytes(unit_bytes)
    installed_unit.chmod(0o644)
    updater.unit_pids[unit_name] = "42"

    proc_root = tmp_path / "proc-verified-c10-broker"
    process = proc_root / "42"
    process.mkdir(parents=True)
    (process / "stat").write_text("42 (cyrene-runtime-maintenance) S 1")
    (process / "cmdline").write_bytes(str(executable).encode() + b"\0")
    (process / "exe").symlink_to(executable)
    return proc_root, executable


def test_partial_c10_resume_allows_owned_kernel_state_but_rejects_foreign_core_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path, c10=True)
    broker_identity = updater.bootstrap_broker_identity
    assert broker_identity is not None
    staged: list[dict[str, Any]] = []
    kernel_executable = ""
    for component_id in bootstrap.C10_FIRST_CORE_COMPONENT_IDS:
        if component_id == "cyrene-runtime-maintenance":
            version = broker_identity["version"]
            manifest_digest = broker_identity["manifestDigest"]
            artifact_digest = broker_identity["artifactDigest"]
        else:
            version = "1.2.3"
            manifest_digest = _digest((component_id + " manifest").encode())
            artifact_digest = _digest((component_id + " artifact").encode())
        entrypoint = "bin/" + component_id
        manifest = {"artifact": {"entrypoint": entrypoint}}
        row = {
            "componentId": component_id,
            "version": version,
            "manifestDigest": manifest_digest,
            "artifactDigest": artifact_digest,
            "manifest": manifest,
        }
        pointer = version + "--" + manifest_digest.removeprefix("sha256:")
        updater.pointers[component_id] = pointer
        staged.append(row)
        if component_id == "cyrene-kernel":
            kernel_executable = str(
                (
                    updater.install_root
                    / "components"
                    / component_id
                    / "releases"
                    / pointer
                    / entrypoint
                ).resolve()
            )

    assert kernel_executable
    updater.core_runtime_root.mkdir(parents=True)
    updater.core_run_root.mkdir(parents=True)
    (updater.core_runtime_root / "journal.jsonl").write_text("owned kernel journal\n")
    (updater.core_run_root / "kernel.sock").touch()
    monkeypatch.setattr(bootstrap, "_verified_running_c10_broker", lambda *_args: broker_identity)
    monkeypatch.setattr(
        bootstrap,
        "_core_process_snapshot",
        lambda *_args, **_kwargs: [("cyrene-kernel", kernel_executable, "88")],
    )

    def runner(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        if argv[:2] == ["systemctl", "show"]:
            return SimpleNamespace(
                returncode=0,
                stdout="88\n" if "MainPID" in argv[2] else "active\n",
            )
        assert argv[0] == "nvidia-smi"
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(updater, "runner", runner)
    assert (
        bootstrap._assert_fresh(
            updater,
            proc_root=tmp_path / "proc-resumed-c10",
            planned_components=staged,
            require_empty_kernel_state=False,
            expected_bootstrap_broker=broker_identity,
        )
        == broker_identity
    )

    foreign_executable = str((tmp_path / "foreign" / "cyrene-kernel").resolve())
    monkeypatch.setattr(
        bootstrap,
        "_core_process_snapshot",
        lambda *_args, **_kwargs: [("cyrene-kernel", foreign_executable, "89")],
    )
    with pytest.raises(ValueError, match="Legacy or manually started Core process"):
        bootstrap._assert_fresh(
            updater,
            proc_root=tmp_path / "proc-resumed-c10",
            planned_components=staged,
            require_empty_kernel_state=False,
            expected_bootstrap_broker=broker_identity,
        )


def test_c10_fresh_check_accepts_only_the_exact_attested_active_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path, c10=True)
    proc_root, executable = _install_c10_broker_fixture(updater, tmp_path)

    # Fixtures run unprivileged; model the root-owned installed files required by production.
    original_lstat = Path.lstat

    def root_owned_lstat(path: Path) -> os.stat_result:
        result = original_lstat(path)
        if path == executable or path.parent in updater.systemd_unit_dirs:
            fields = list(result)
            fields[4] = 0
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "lstat", root_owned_lstat)
    installed_unit = updater.systemd_unit_dirs[0] / "cyrene-runtime-maintenance.service"
    assert installed_unit.lstat().st_uid == 0
    assert installed_unit.lstat().st_nlink == 1
    assert not (installed_unit.lstat().st_mode & 0o022)
    verified = bootstrap._verified_running_c10_broker(updater, proc_root)
    assert verified == updater.bootstrap_broker_identity
    assert Path(verified["executable"]) == executable
    assert bootstrap._assert_fresh(updater, proc_root=proc_root) == verified


def test_c10_fresh_check_rejects_a_state_one_broker_receipt(tmp_path: Path) -> None:
    updater = FakeUpdater(tmp_path, c10=True)
    updater.initial_broker_manifest["schemaVersion"] = 1
    with pytest.raises(ValueError, match="exact C10 contract"):
        bootstrap._assert_fresh(updater, proc_root=_fake_proc(tmp_path / "proc-state-one"))


@pytest.mark.parametrize("defect", ["foreign-live-broker", "mainpid-executable-mismatch"])
def test_c10_fresh_check_rejects_unknown_or_mismatched_broker_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    updater = FakeUpdater(tmp_path, c10=True)
    proc_root, executable = _install_c10_broker_fixture(updater, tmp_path)

    original_lstat = Path.lstat

    def root_owned_lstat(path: Path) -> os.stat_result:
        result = original_lstat(path)
        if path == executable or path.parent in updater.systemd_unit_dirs:
            fields = list(result)
            fields[4] = 0
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "lstat", root_owned_lstat)
    if defect == "foreign-live-broker":
        foreign = proc_root / "43"
        foreign.mkdir()
        foreign_executable = tmp_path / "foreign" / "cyrene-runtime-maintenance"
        foreign_executable.parent.mkdir()
        foreign_executable.write_bytes(b"untrusted executable")
        (foreign / "stat").write_text("43 (cyrene-runtime-maintenance) S 1")
        (foreign / "cmdline").write_bytes(str(foreign_executable).encode() + b"\0")
        (foreign / "exe").symlink_to(foreign_executable)
        expected = "Legacy or manually started Core process"
    else:
        foreign_executable = tmp_path / "foreign" / "cyrene-runtime-maintenance"
        foreign_executable.parent.mkdir()
        foreign_executable.write_bytes(b"untrusted executable")
        (proc_root / "42" / "exe").unlink()
        (proc_root / "42" / "exe").symlink_to(foreign_executable)
        expected = "not its signed executable"
    with pytest.raises((RuntimeError, ValueError), match=expected):
        bootstrap._assert_fresh(updater, proc_root=proc_root)


def test_c10_broker_hold_token_is_journaled_before_broker_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path, c10=True)
    broker_identity = updater.bootstrap_broker_identity
    assert broker_identity is not None
    candidate_digest = _digest(b"candidate broker manifest")
    candidate = {
        "componentId": "cyrene-runtime-maintenance",
        "version": "1.0.0",
        "manifestDigest": candidate_digest,
        "artifactDigest": _digest(b"candidate broker artifact"),
        "manifest": {},
    }
    transaction = {
        "phase": "installing",
        "maintenanceToken": "t" * 40,
        "bootstrapBroker": broker_identity,
        "components": [candidate],
        "brokerRestartPhase": None,
    }
    journal_path = bootstrap._journal_path(updater)
    monkeypatch.setattr(
        bootstrap,
        "_verified_running_c10_broker",
        lambda *_args: broker_identity,
    )
    monkeypatch.setattr(bootstrap, "_core_process_snapshot", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(bootstrap, "_remove_verified_broker_units", lambda *_args, **_kwargs: None)
    original_stop = updater._run_systemctl

    def stop_with_journal_check(operation: str, unit: str) -> None:
        if operation == "stop":
            persisted = json.loads(journal_path.read_text())
            assert persisted["maintenanceToken"] == "t" * 40
            assert persisted["brokerRestartPhase"] == "stop_pending"
        original_stop(operation, unit)

    monkeypatch.setattr(updater, "_run_systemctl", stop_with_journal_check)
    bootstrap._stop_initial_c10_broker(updater, transaction, journal_path)

    assert transaction["brokerRestartPhase"] == "stopped"
    assert json.loads(journal_path.read_text())["maintenanceToken"] == "t" * 40


def test_c10_broker_pid_must_match_the_exact_live_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc_root = _fake_proc(tmp_path / "proc-c10-broker-process", core="cyrene-runtime-maintenance")
    executable = Path(os.readlink(proc_root / "100" / "exe"))
    bootstrap._verify_process_executable(proc_root, "100", executable, description="Test Broker")
    with pytest.raises(ValueError, match="not its signed executable"):
        bootstrap._verify_process_executable(
            proc_root,
            "100",
            executable.with_name("untrusted-broker"),
            description="Test Broker",
        )

    original_readlink = os.readlink
    exe_link = proc_root / "100" / "exe"

    def deleted_broker_link(path: Any, *args: Any, **kwargs: Any) -> str:
        target = original_readlink(path, *args, **kwargs)
        return target + " (deleted)" if Path(path) == exe_link else target

    monkeypatch.setattr(os, "readlink", deleted_broker_link)
    with pytest.raises(ValueError, match="not its signed executable"):
        bootstrap._verify_process_executable(
            proc_root, "100", executable, description="Test Broker"
        )


def test_c10_fresh_check_allows_only_verified_broker_and_rejects_other_core_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path, c10=True)
    monkeypatch.setattr(
        bootstrap,
        "_verified_running_c10_broker",
        lambda *_args: updater.bootstrap_broker_identity,
    )
    proc_root = _fake_proc(tmp_path / "proc-c10-foreign-core", core="cyrene-kernel")
    with pytest.raises(ValueError, match="Legacy or manually started Core process"):
        bootstrap._assert_fresh(updater, proc_root=proc_root)


@pytest.mark.parametrize(
    "field,value,expected_error",
    [
        ("protocol_version", "cyrene.runtime-maintenance.broker.v0", "state-v2 contract"),
        ("capabilities", [], "state-v2 contract"),
        ("core_bootstrap_eligible", False, "fresh first-Core bootstrap state"),
    ],
)
def test_c10_health_requires_state_v2_and_fresh_bootstrap_eligibility(
    tmp_path: Path, field: str, value: Any, expected_error: str
) -> None:
    updater = FakeUpdater(tmp_path, c10=True)
    original_request = updater._broker_request

    def invalid_health(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        result = original_request(method, params, **kwargs)
        if method == "Health":
            result[field] = value
        return result

    updater._broker_request = invalid_health
    with pytest.raises(RuntimeError, match=expected_error):
        bootstrap._health_snapshot(updater)


def test_products_opt_in_is_boolean_and_status_does_not_claim_product_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    monkeypatch.setattr(bootstrap, "_assert_fresh", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bootstrap,
        "_health_snapshot",
        lambda *_args, **_kwargs: {
            "coreBootstrapEligible": True,
            "gateGeneration": 9,
            "catalogGeneration": 1,
            "activitySources": ["source-a", "source-b"],
        },
    )
    result = bootstrap.handle(
        updater,
        {
            "protocolVersion": updates.PROTOCOL_VERSION,
            "operation": "status",
            "bootstrapMode": bootstrap.CORE_BOOTSTRAP_MODE,
            "includeProducts": True,
        },
    )
    assert result["includeProducts"] is True
    assert "firstProducts" not in result
    assert not any(event[0] == "products-check" for event in updater.events)
    with pytest.raises(TypeError, match="boolean"):
        bootstrap.handle(
            updater,
            {
                "protocolVersion": updates.PROTOCOL_VERSION,
                "operation": "check",
                "bootstrapMode": bootstrap.CORE_BOOTSTRAP_MODE,
                "includeProducts": "true",
            },
        )


def test_products_identity_is_bound_by_plan_stage_and_explicit_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    products = FakeFirstProducts(updater)
    monkeypatch.setattr(bootstrap, "_first_products_module", lambda: products)
    proc = _fake_proc(tmp_path / "proc-products-plan")
    plan = bootstrap.check(updater, include_products=True, proc_root=proc)["plan"]
    assert plan["includeProducts"] is True
    assert plan["firstProducts"] == products.identity
    assert bootstrap._load_plan(updater, plan["planId"], plan["planDigest"]) == plan

    staged = bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    assert staged["firstProducts"]["receiptDigest"] == plan["firstProducts"]["receiptDigest"]
    assert any(event[0] == "products-stage" for event in updater.events)
    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    confirmation = _product_confirmation(plan)
    changed = json.loads(json.dumps(confirmation))
    changed["firstProducts"]["products"][0]["artifactDigest"] = _digest(b"tampered")
    with pytest.raises(ValueError, match="confirmation"):
        bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            changed,
            proc_root=_fake_proc(tmp_path / "proc-products-tamper"),
        )


def test_product_api_activation_occurs_under_core_hold_and_post_end_retry_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    products = FakeFirstProducts(updater)
    monkeypatch.setattr(bootstrap, "_first_products_module", lambda: products)
    plan = bootstrap.check(
        updater,
        include_products=True,
        proc_root=_fake_proc(tmp_path / "proc-products-apply"),
    )["plan"]
    bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    monkeypatch.setattr(bootstrap, "_verify_started_processes", lambda *_: None)
    monkeypatch.setattr(bootstrap, "_verify_live_core_cohort", lambda *_: None)
    confirmation = _product_confirmation(plan)

    first = bootstrap.apply(
        updater,
        plan["planId"],
        plan["planDigest"],
        confirmation,
        proc_root=_fake_proc(tmp_path / "proc-products-apply-again"),
    )
    hold = next(
        i for i, event in enumerate(updater.events) if event[:2] == ("broker", "BeginCoreBootstrap")
    )
    activate = next(
        i for i, event in enumerate(updater.events) if event[0] == "products-activate-held"
    )
    end = next(i for i, event in enumerate(updater.events) if event[0] == "end")
    assert hold < activate < end
    assert first["status"] == "post-end-pending"
    assert json.loads(bootstrap._journal_path(updater).read_text())["phase"] == "post_end_pending"
    monkeypatch.setattr(bootstrap, "_assert_fresh", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bootstrap,
        "_health_snapshot",
        lambda *_args, **_kwargs: {
            "coreBootstrapEligible": False,
            "gateGeneration": 10,
            "catalogGeneration": 1,
            "activitySources": ["source-a", "source-b"],
        },
    )
    status = bootstrap.handle(
        updater,
        {
            "protocolVersion": updates.PROTOCOL_VERSION,
            "operation": "status",
            "bootstrapMode": bootstrap.CORE_BOOTSTRAP_MODE,
            "includeProducts": True,
        },
    )
    assert status["firstProductsBootstrap"] == {
        "status": "pending",
        "phase": "post_end_pending",
        "gateReleaseConfirmed": True,
    }

    resumed = bootstrap.apply(
        updater,
        plan["planId"],
        plan["planDigest"],
        confirmation,
        proc_root=_fake_proc(tmp_path / "proc-products-recover"),
    )
    assert resumed["status"] == "installed"
    assert resumed["firstProducts"]["status"] == "complete"
    assert sum(event[0] == "end" for event in updater.events) == 1
    assert sum(event[0] == "products-recover-post-end" for event in updater.events) == 1
    assert not any(event[0] == "stop" for event in updater.events)


def test_unknown_kernel_counts_refuse_product_activation_without_claiming_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    products = FakeFirstProducts(updater)
    monkeypatch.setattr(bootstrap, "_first_products_module", lambda: products)
    plan = bootstrap.check(
        updater,
        include_products=True,
        proc_root=_fake_proc(tmp_path / "proc-products-unknown"),
    )["plan"]
    bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    updater.gate_counts.update(
        {
            "blocker_codes": ["RUNTIME_ACTIVITY_UNKNOWN"],
            "active_task_count": None,
            "inflight_runtime_admission_count": None,
            "active_worker_count": None,
            "active_allocation_count": None,
        }
    )
    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    monkeypatch.setattr(bootstrap, "_verify_started_processes", lambda *_: None)
    monkeypatch.setattr(bootstrap, "_verify_live_core_cohort", lambda *_: None)
    with pytest.raises(RuntimeError, match="hold remains closed"):
        bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            _product_confirmation(plan),
            proc_root=_fake_proc(tmp_path / "proc-products-unknown-apply"),
        )
    assert not any(event[0] == "products-activate-held" for event in updater.events)
    assert not any(event[0] == "products-rollback-pre-end" for event in updater.events)
    assert not any(event[0] == "end" for event in updater.events)


def test_owned_product_cleanup_precedes_core_cleanup_before_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    products = FakeFirstProducts(updater)
    products.make_runtime_unknown_after_activate = True
    for component_id in bootstrap.CORE_COMPONENT_IDS:
        updater.unit_pids[updater.components[component_id]["systemdUnit"]] = "55"
    monkeypatch.setattr(bootstrap, "_first_products_module", lambda: products)
    plan = bootstrap.check(
        updater,
        include_products=True,
        proc_root=_fake_proc(tmp_path / "proc-product-cleanup"),
    )["plan"]
    bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    monkeypatch.setattr(bootstrap, "_verify_started_processes", lambda *_: None)
    monkeypatch.setattr(bootstrap, "_verify_live_core_cohort", lambda *_: None)
    monkeypatch.setattr(bootstrap, "_verify_candidate_pid", lambda *_, **__: None)

    with pytest.raises(RuntimeError, match="hold remains closed"):
        bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            _product_confirmation(plan),
            proc_root=_fake_proc(tmp_path / "proc-product-cleanup-apply"),
        )
    product_cleanup = next(
        i for i, event in enumerate(updater.events) if event[0] == "products-rollback-pre-end"
    )
    core_stop = next(i for i, event in enumerate(updater.events) if event[0] == "stop")
    assert product_cleanup < core_stop
    assert all(pointer is None for pointer in updater.pointers.values())
    assert not any(event[0] == "end" for event in updater.events)


def test_uncertain_product_end_with_unhealthy_recovery_preserves_both_cohorts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    products = FakeFirstProducts(updater)
    monkeypatch.setattr(bootstrap, "_first_products_module", lambda: products)
    plan = bootstrap.check(
        updater,
        include_products=True,
        proc_root=_fake_proc(tmp_path / "proc-products-uncertain"),
    )["plan"]
    bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    monkeypatch.setattr(bootstrap, "_verify_started_processes", lambda *_: None)
    monkeypatch.setattr(bootstrap, "_verify_live_core_cohort", lambda *_: None)
    confirmation = _product_confirmation(plan)
    original_request = updater._broker_request
    end_attempts = 0

    def lose_end_response(
        method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        nonlocal end_attempts
        if method == "EndMaintenance":
            end_attempts += 1
            raise RuntimeError("response lost after dispatch")
        return original_request(method, params, request_id=request_id)

    updater._broker_request = lose_end_response
    with pytest.raises(RuntimeError, match="result is uncertain"):
        bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            confirmation,
            proc_root=_fake_proc(tmp_path / "proc-products-end-first"),
        )
    assert json.loads(bootstrap._journal_path(updater).read_text())["phase"] == "end_call_pending"

    updater.gate_counts["active_worker_count"] = None
    with pytest.raises(RuntimeError, match="outcome is uncertain"):
        bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            confirmation,
            proc_root=_fake_proc(tmp_path / "proc-products-end-retry"),
        )
    journal = json.loads(bootstrap._journal_path(updater).read_text())
    assert journal["phase"] == "end_call_pending"
    assert journal["firstProducts"]["phase"] == "active"
    assert end_attempts == 1
    assert all(pointer is not None for pointer in updater.pointers.values())
    assert not any(event[0] == "products-rollback-pre-end" for event in updater.events)
    assert not any(event[0] == "stop" for event in updater.events)


def test_check_rejects_nonfresh_catalog_target_and_gpu_resources(tmp_path: Path) -> None:
    updater = FakeUpdater(tmp_path)
    updater.core_bootstrap_eligible = False
    with pytest.raises(RuntimeError, match="fresh first-Core"):
        bootstrap.check(updater, proc_root=_fake_proc(tmp_path / "proc-not-fresh"))

    updater = FakeUpdater(tmp_path / "incompatible")
    original_target = updater._target_for
    updater._target_for = lambda component: {
        "id": "other-target"
        if component["componentId"] == "cyrene-kernel"
        else original_target(component)["id"]
    }
    with pytest.raises(ValueError, match="one supported native target"):
        bootstrap.check(updater, proc_root=_fake_proc(tmp_path / "proc-incompatible"))

    updater = FakeUpdater(tmp_path / "gpu-busy")
    updater.gpu_output = "1234"
    with pytest.raises(ValueError, match="GPU compute resources"):
        bootstrap.check(updater, proc_root=_fake_proc(tmp_path / "proc-gpu-busy"))


def test_confirmation_binds_full_plan_and_rejects_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    plan = _check_plan(updater, tmp_path)
    bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    confirmation = {
        "mode": bootstrap.CORE_BOOTSTRAP_MODE,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": dict(plan["componentArtifactDigests"]),
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
    }
    changed = dict(confirmation, componentArtifactDigests={"cyrene-kernel": "sha256:" + "0" * 64})
    with pytest.raises(ValueError, match="confirmation"):
        bootstrap.apply(updater, plan["planId"], plan["planDigest"], changed)
    plan_path = bootstrap._plan_path(updater, plan["planId"])
    stored = json.loads(plan_path.read_text())
    stored["componentArtifactDigests"]["cyrene-kernel"] = "sha256:" + "0" * 64
    plan_path.write_text(json.dumps(stored))
    confirmation = dict(confirmation, componentArtifactDigests=stored["componentArtifactDigests"])
    with pytest.raises(ValueError, match="artifact digest map"):
        bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            confirmation,
            proc_root=_fake_proc(tmp_path / "proc-tamper"),
        )


def test_closed_hold_precedes_pointer_activation_and_services_start_in_catalog_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    plan = _check_plan(updater, tmp_path)
    bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    monkeypatch.setattr(bootstrap, "_verify_started_processes", lambda *_: None)
    confirmation = {
        "mode": bootstrap.CORE_BOOTSTRAP_MODE,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
    }
    result = bootstrap.apply(
        updater,
        plan["planId"],
        plan["planDigest"],
        confirmation,
        proc_root=_fake_proc(tmp_path / "proc-apply"),
    )
    events = updater.events
    hold = next(
        i for i, event in enumerate(events) if event[:2] == ("broker", "BeginCoreBootstrap")
    )
    activation = next(i for i, event in enumerate(events) if event[0] == "activate")
    starts = [(i, event) for i, event in enumerate(events) if event[0] == "start"]
    ready = next(i for i, event in enumerate(events) if event[0] == "readiness")
    end = next(i for i, event in enumerate(events) if event[0] == "end")
    assert hold < activation < starts[0][0] < ready < end
    assert result["status"] == "installed"
    assert [event[1] for _, event in starts] == [
        updater.components[item]["systemdUnit"] for item in bootstrap.CORE_COMPONENT_IDS
    ]
    first_component = bootstrap.CORE_COMPONENT_IDS[0]
    first_unit = updater.components[first_component]["systemdUnit"]
    assert (updater.systemd_unit_dirs[0] / first_unit).is_file()
    assert not (updater.systemd_unit_dirs[1] / first_unit).exists()
    assert not (updater.systemd_unit_dirs[2] / first_unit).exists()


def test_unknown_real_kernel_counts_preserve_hold_and_clean_candidate_pointers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    updater.gate_counts = {
        "status": "UNKNOWN",
        "blocker_codes": ["RUNTIME_ACTIVITY_UNKNOWN"],
        "active_task_count": None,
        "active_tasks": [],
        "inflight_runtime_admission_count": None,
        "active_worker_count": None,
        "active_allocation_count": None,
    }
    plan = _check_plan(updater, tmp_path)
    bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    monkeypatch.setattr(bootstrap, "_verify_started_processes", lambda *_: None)
    confirmation = {
        "mode": bootstrap.CORE_BOOTSTRAP_MODE,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
    }
    with pytest.raises(RuntimeError, match="hold remains closed"):
        bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            confirmation,
            proc_root=_fake_proc(tmp_path / "proc-apply"),
        )
    assert all(pointer is None for pointer in updater.pointers.values())
    assert not any(event[0] == "end" for event in updater.events)
    journal = json.loads(bootstrap._journal_path(updater).read_text())
    assert journal["phase"] == "hold_required"
    assert journal["maintenanceToken"] == "t" * 40


def test_failure_stops_only_candidate_units_in_reverse_order_before_pointer_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    updater.gate_counts["active_worker_count"] = None
    for component_id in bootstrap.CORE_COMPONENT_IDS:
        updater.unit_pids[updater.components[component_id]["systemdUnit"]] = "55"
    plan = _check_plan(updater, tmp_path)
    bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    monkeypatch.setattr(bootstrap, "_verify_started_processes", lambda *_: None)
    monkeypatch.setattr(bootstrap, "_verify_candidate_pid", lambda *_, **__: None)
    confirmation = {
        "mode": bootstrap.CORE_BOOTSTRAP_MODE,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
    }
    with pytest.raises(RuntimeError, match="hold remains closed"):
        bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            confirmation,
            proc_root=_fake_proc(tmp_path / "proc-failure"),
        )
    stopped = [event[1] for event in updater.events if event[0] == "stop"]
    expected = [
        updater.components[item]["systemdUnit"] for item in reversed(bootstrap.CORE_COMPONENT_IDS)
    ]
    assert stopped == expected, json.loads(bootstrap._journal_path(updater).read_text()).get(
        "cleanupError"
    )
    assert all(pointer is None for pointer in updater.pointers.values())


def test_end_call_pending_retry_rechecks_real_kernel_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    plan = _check_plan(updater, tmp_path)
    bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    monkeypatch.setattr(bootstrap, "_verify_live_core_cohort", lambda *_: None)
    monkeypatch.setattr(bootstrap, "_verify_started_processes", lambda *_: None)
    original_request = updater._broker_request
    end_attempts = 0

    def uncertain_end(
        method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        nonlocal end_attempts
        if method == "EndMaintenance":
            end_attempts += 1
            raise RuntimeError("lost response")
        return original_request(method, params, request_id=request_id)

    updater._broker_request = uncertain_end
    confirmation = {
        "mode": bootstrap.CORE_BOOTSTRAP_MODE,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
    }
    with pytest.raises(RuntimeError, match="result is uncertain"):
        bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            confirmation,
            proc_root=_fake_proc(tmp_path / "proc-end-1"),
        )
    assert json.loads(bootstrap._journal_path(updater).read_text())["phase"] == "end_call_pending"

    updater.gate_counts["active_allocation_count"] = None
    with pytest.raises(RuntimeError, match="outcome is uncertain"):
        bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            confirmation,
            proc_root=_fake_proc(tmp_path / "proc-end-2"),
        )
    assert end_attempts == 1
    assert json.loads(bootstrap._journal_path(updater).read_text())["phase"] == "end_call_pending"
    assert all(pointer is not None for pointer in updater.pointers.values())
    assert not any(event[0] == "stop" for event in updater.events)


def test_successful_end_is_never_rolled_back_when_final_journal_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    plan = _check_plan(updater, tmp_path)
    bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    monkeypatch.setattr(bootstrap, "_verify_live_core_cohort", lambda *_: None)
    monkeypatch.setattr(bootstrap, "_verify_started_processes", lambda *_: None)
    confirmation = {
        "mode": bootstrap.CORE_BOOTSTRAP_MODE,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
    }
    write_private = bootstrap._write_private_json
    failed = False

    def fail_after_end(owner: Any, path: Path, value: dict[str, Any]) -> None:
        nonlocal failed
        if value.get("phase") == "end_confirmed" and not failed:
            failed = True
            raise OSError("simulated durable journal write failure")
        write_private(owner, path, value)

    monkeypatch.setattr(bootstrap, "_write_private_json", fail_after_end)
    with pytest.raises(OSError, match="journal write failure"):
        bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            confirmation,
            proc_root=_fake_proc(tmp_path / "proc-finalize"),
        )
    assert all(pointer is not None for pointer in updater.pointers.values())
    assert not any(event[0] == "stop" for event in updater.events)
    assert json.loads(bootstrap._journal_path(updater).read_text())["phase"] == "end_call_pending"

    monkeypatch.setattr(bootstrap, "_write_private_json", write_private)
    result = bootstrap.apply(
        updater,
        plan["planId"],
        plan["planDigest"],
        confirmation,
        proc_root=_fake_proc(tmp_path / "proc-recover-finalize"),
    )
    assert result["status"] == "installed"
    assert all(pointer is not None for pointer in updater.pointers.values())
    assert not any(event[0] == "stop" for event in updater.events)


def test_abnormal_begin_pending_reuses_same_request_and_refuses_other_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = FakeUpdater(tmp_path)
    plan = _check_plan(updater, tmp_path)
    bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    path = bootstrap._journal_path(updater)
    base_stage = json.loads(
        (updater.state_root / "staged" / plan["basePlanId"] / "stage.json").read_text()
    )
    components = base_stage["components"]
    begin_request = {
        "request_id": plan["requestId"],
        "target_kind": "CORE_RUNTIME",
        "requires_restart": True,
        "user_confirmed_restart": True,
        "expected_gate_generation": plan["gateGeneration"],
        "expected_catalog_generation": plan["catalogGeneration"],
        "expected_activity_sources": plan["activitySources"],
        "plan_id": plan["planId"],
        "plan_digest": plan["planDigest"],
        "component_artifact_digests": plan["componentArtifactDigests"],
    }
    bootstrap._write_private_json(
        updater,
        path,
        {
            "requestId": plan["requestId"],
            "planId": plan["planId"],
            "planDigest": plan["planDigest"],
            "targetKind": "CORE_RUNTIME",
            "expectedGateGeneration": plan["gateGeneration"],
            "expectedCatalogGeneration": plan["catalogGeneration"],
            "expectedActivitySources": plan["activitySources"],
            "componentArtifactDigests": plan["componentArtifactDigests"],
            "components": components,
            "previous": updater._capture_active_versions(components),
            "beginRequest": begin_request,
            "phase": "begin_pending",
        },
    )
    monkeypatch.setattr(bootstrap, "_verify_started_processes", lambda *_: None)
    confirmation = {
        "mode": bootstrap.CORE_BOOTSTRAP_MODE,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
    }
    bootstrap.apply(
        updater,
        plan["planId"],
        plan["planDigest"],
        confirmation,
        proc_root=_fake_proc(tmp_path / "proc-apply"),
    )
    assert sum(event[:2] == ("broker", "BeginCoreBootstrap") for event in updater.events) == 1


def test_deb_installs_the_fixed_core_bootstrap_helper() -> None:
    build_script = (ROOT / "packaging" / "build-deb.sh").read_text(encoding="utf-8")
    assert '"${SCRIPT_DIR}/native_core_bootstrap.py"' in build_script
    assert '"${STAGE_DIR}/usr/lib/cyrene/scripts/native_core_bootstrap.py"' in build_script


def test_fixed_four_operation_helper_routes_only_the_named_bootstrap_mode(tmp_path: Path) -> None:
    updater = FakeUpdater(tmp_path)
    request = {
        "protocolVersion": updates.PROTOCOL_VERSION,
        "operation": "check",
        "bootstrapMode": bootstrap.CORE_BOOTSTRAP_MODE,
    }
    result = updates.ComponentUpdater.handle(updater, request)
    # This container cannot inspect PID 1's executable; the request was routed
    # into the fixed helper and rejected with the intended fail-closed envelope.
    assert result["ok"] is False
    assert result["error"]["code"] == "INTERNAL_ERROR"
    invalid = updates.ComponentUpdater.handle(
        updater, {**request, "bootstrapMode": "arbitrary-root-mode"}
    )
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "INVALID_REQUEST"

    status = updates.ComponentUpdater.handle(
        updater,
        {
            "protocolVersion": updates.PROTOCOL_VERSION,
            "operation": "status",
            "bootstrapMode": bootstrap.CORE_BOOTSTRAP_MODE,
            "includeProducts": True,
        },
    )
    assert status["ok"] is True
    assert status["result"]["includeProducts"] is True
    wrong_type = updates.ComponentUpdater.handle(
        updater,
        {
            "protocolVersion": updates.PROTOCOL_VERSION,
            "operation": "check",
            "bootstrapMode": bootstrap.CORE_BOOTSTRAP_MODE,
            "includeProducts": "true",
        },
    )
    assert wrong_type["ok"] is False
    assert wrong_type["error"]["code"] == "INVALID_REQUEST"
    unscoped = updates.ComponentUpdater.handle(
        updater,
        {
            "protocolVersion": updates.PROTOCOL_VERSION,
            "operation": "status",
            "includeProducts": True,
        },
    )
    assert unscoped["ok"] is False
    assert unscoped["error"]["code"] == "INVALID_REQUEST"


def test_fresh_workload_plan_delegates_active_v2_authority_validation(
    tmp_path: Path,
) -> None:
    updater = FakeUpdater(tmp_path, c10=True)
    active_digest = _digest(b"signed active C10 catalog generation 15")
    updater.catalog_digest = active_digest
    updater.bootstrap_catalog_digest = active_digest
    group = updater.catalog["compatibilityGroups"][0]
    component_rows = []
    for component_id in bootstrap.C10_FIRST_CORE_COMPONENT_IDS:
        digest = _digest(component_id.encode("utf-8"))
        manifest_digest = _digest((component_id + "-manifest").encode("utf-8"))
        row: dict[str, Any] = {
            "componentId": component_id,
            "version": "1.2.3",
            "manifestDigest": manifest_digest,
            "artifactDigest": digest,
            "targetId": "linux-ubuntu-24.04-x86_64-systemd",
        }
        protocol = bootstrap._PACKAGE_RUNTIME_PROTOCOLS.get(component_id)
        if protocol is not None:
            row["manifest"] = {
                "schemaVersion": 2,
                "protocolVersion": protocol,
                "compatibility": {
                    "groupId": group["groupId"],
                    "groupVersion": group["groupVersion"],
                    "contractApiVersion": group["contractApiVersion"],
                    "wireApiVersion": group["wireApiVersion"],
                    "contractLock": group["contractLock"],
                },
            }
        component_rows.append(row)
    digests = {row["componentId"]: row["artifactDigest"] for row in component_rows}
    full_digests = {**digests, "cyrene-plugin-example": _digest(b"plugin")}
    broker_digest = _digest(b"offline Broker plan")
    source_policy = {
        "mode": "standaloneOperator",
        "sourceId": "cyrene-plugin-standalone-operator",
    }
    child_material = {
        "schemaVersion": 1,
        "cohortId": "C10",
        "workloadId": "test-workload",
        "channel": "stable",
        "catalogDigest": active_digest,
        "targetId": "linux-ubuntu-24.04-x86_64-systemd",
        "sourcePolicy": source_policy,
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
            for row in component_rows
        ],
        "componentArtifactDigests": digests,
        "maintenanceComponentArtifactDigests": full_digests,
        "brokerBootstrapPlanDigest": broker_digest,
    }
    plan_digest = _digest(
        json.dumps(
            child_material,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    block = {
        "schemaVersion": 1,
        "cohortId": "C10",
        "planId": "plan-" + plan_digest.removeprefix("sha256:")[:32],
        "planDigest": plan_digest,
        "targetId": "linux-ubuntu-24.04-x86_64-systemd",
        "catalogDigest": active_digest,
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
            for row in component_rows
        ],
        "componentArtifactDigests": digests,
        "maintenanceComponentArtifactDigests": full_digests,
    }
    parent_plan = {
        "channel": "stable",
        "firstCoreBootstrap": block,
        "resolution": {
            "sourcePolicy": source_policy,
            "planDigestMaterial": {
                "firstCoreBootstrapInternal": {
                    "brokerBootstrapPlanDigest": broker_digest,
                    "childPlanDigestMaterial": child_material,
                }
            },
        },
    }

    validated_catalogs: list[str] = []

    def validate_active_v2_context(actual_updater: Any, digest: str) -> None:
        assert actual_updater is updater
        assert actual_updater.bootstrap_catalog_digest == active_digest
        validated_catalogs.append(digest)

    bootstrap_module = SimpleNamespace(
        _require_active_v2_catalog_context=validate_active_v2_context
    )
    plan, checked_block, full_digests = bootstrap._fresh_workload_core_plan(
        updater, parent_plan, component_rows, "stable", bootstrap_module
    )

    assert checked_block == block
    assert validated_catalogs == [active_digest]
    assert plan["requestId"] == "first-core-bootstrap-" + block["planId"].removeprefix("plan-")
    assert plan["brokerBootstrapPlanDigest"] == broker_digest
    assert plan["componentArtifactDigests"] == block["maintenanceComponentArtifactDigests"]
    assert full_digests == block["maintenanceComponentArtifactDigests"]


def test_fresh_workload_hold_validation_binds_full_map_at_generation_one() -> None:
    source_id = "cyrene-plugin-standalone-operator"
    source_uid, source_gid = 12001, 12002
    component_digests = {
        component_id: _digest(component_id.encode("utf-8"))
        for component_id in bootstrap.C10_FIRST_CORE_COMPONENT_IDS
    }
    component_digests["cyrene-plugin-example"] = _digest(b"selected plugin")
    plan = {
        "requestId": "first-core-bootstrap-" + "a" * 32,
        "planId": "plan-" + "a" * 32,
        "planDigest": _digest(b"fresh workload plan"),
    }
    transaction = {
        "maintenanceToken": "held-token",
        "maintenanceGateGeneration": 14,
        "progress": "cohort_installing",
    }

    class HoldUpdater:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def _activity_catalog(
            self, *, allow_uninitialized: bool = False
        ) -> tuple[dict[str, Any], list[str]]:
            assert allow_uninitialized is True
            return (
                {
                    "schema_version": 1,
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
            )

        def _broker_request(
            self, method: str, params: dict[str, Any], *, request_id: str | None = None
        ) -> dict[str, Any]:
            assert method == "ValidateMaintenanceHold"
            assert request_id == plan["requestId"]
            self.calls.append(params)
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

    updater = HoldUpdater()
    generation = bootstrap._fresh_workload_core_validate_maintenance_hold(
        updater,
        plan,
        transaction,
        component_digests,
        source_id=source_id,
        source_uid=source_uid,
        source_gid=source_gid,
    )

    assert generation == 1
    assert len(updater.calls) == len(component_digests)
    assert {call["component_id"] for call in updater.calls} == set(component_digests)
    assert all(call["component_artifact_digests"] == component_digests for call in updater.calls)
    assert all(call["maintenance_token"] == "held-token" for call in updater.calls)


@pytest.mark.parametrize(
    ("progress", "requires_empty_state"),
    [
        ("held", True),
        ("catalog_initialization_pending", True),
        ("catalog_initialized", True),
        ("cohort_installing", False),
        ("cohort_activated", False),
        ("cohort_starting", False),
        ("cohort_started", False),
        ("readiness_pending", False),
    ],
)
def test_fresh_workload_resume_state_depends_on_durable_cohort_progress(
    progress: str, requires_empty_state: bool
) -> None:
    assert (
        bootstrap._fresh_workload_core_requires_empty_kernel_state(progress) is requires_empty_state
    )


def test_fresh_workload_resume_rejects_unknown_held_progress() -> None:
    with pytest.raises(ValueError, match="unsupported held progress"):
        bootstrap._fresh_workload_core_requires_empty_kernel_state("unknown")
