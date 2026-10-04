"""Failure-closed fixtures for the first managed Core install path."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path
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

    def __init__(self, root: Path) -> None:
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
        self.components = {
            component_id: {
                "componentId": component_id,
                "systemdUnit": component_id + ".service",
                "restart": {
                    "group": "core-runtime",
                    "order": order,
                    "unit": component_id + ".service",
                },
            }
            for order, component_id in enumerate(bootstrap.CORE_COMPONENT_IDS, start=10)
        }
        self.targets = {"target-ubuntu": {"id": "target-ubuntu", "target": "linux-ubuntu-test"}}
        self.catalog_generation = 4
        self.catalog_digest = _digest(b"catalog")
        self.core_bootstrap_eligible = True
        self.gate_generation = 9
        self.gpu_output = ""
        self.events: list[Any] = []
        self.pointers: dict[str, str | None] = dict.fromkeys(bootstrap.CORE_COMPONENT_IDS)
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
        self.unit_pids: dict[str, str] = {}
        self.stopped_units: set[str] = set()

    def _reload_catalog_for_operation(self) -> None:
        pass

    def _require_authorized_process(self) -> None:
        pass

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

    def _target_for(self, component: dict[str, Any]) -> dict[str, Any]:
        return self.targets["target-ubuntu"]

    def _broker_request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        self.events.append(("broker", method, params))
        if method == "Health":
            return {
                "status": "SERVING",
                "catalog_generation": 1,
                "gate_generation": self.gate_generation,
                "core_bootstrap_eligible": self.core_bootstrap_eligible,
            }
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

    def check(
        self, component_ids: list[str], *, channel: Any = None, include_readiness: bool = True
    ) -> dict[str, Any]:
        components = [
            {
                "componentId": component_id,
                "version": "1.0.0",
                "manifestDigest": _digest(component_id.encode()),
                "artifactDigest": _digest((component_id + " artifact").encode()),
                "restartGroup": "core-runtime",
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
            (release / "systemd" / unit_path.removeprefix("systemd/")).write_bytes(unit)
            manifest = {
                "componentId": component_id,
                "version": item["version"],
                "manifestDigest": item["manifestDigest"],
                "artifact": {"entrypoint": binary_path},
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
                "identityAttested": False,
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
        if command and command[:2] == ["systemctl", "show"]:
            unit = command[-1]
            stdout = "0" if unit in self.stopped_units else self.unit_pids.get(unit, self.main_pid)
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
    monkeypatch.setattr(bootstrap, "_verify_candidate_pid", lambda *_: None)
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
    assert stopped == expected
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
