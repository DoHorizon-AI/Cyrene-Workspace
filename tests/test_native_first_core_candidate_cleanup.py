"""Regression coverage for exact and bounded first-Core candidate cleanup."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "native_first_core_candidate_cleanup", ROOT / "packaging" / "native_core_bootstrap.py"
)
assert spec is not None and spec.loader is not None
bootstrap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bootstrap
spec.loader.exec_module(bootstrap)
held_recovery_spec = importlib.util.spec_from_file_location(
    "native_first_core_candidate_cleanup_held_recovery",
    ROOT / "tests" / "test_native_first_core_held_recovery.py",
)
assert held_recovery_spec is not None and held_recovery_spec.loader is not None
held_recovery = importlib.util.module_from_spec(held_recovery_spec)
sys.modules[held_recovery_spec.name] = held_recovery
held_recovery_spec.loader.exec_module(held_recovery)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class VirtualClock:
    """Advance deterministically whenever the bounded cleanup loop sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.now += duration


class UnitUpdater:
    """Model one systemd manager and the exact staged C9 cohort."""

    def __init__(self, root: Path) -> None:
        self.install_root = root / "usr-lib-cyrene"
        self.systemd_unit_dirs = (
            root / "etc-systemd",
            root / "lib-systemd",
            root / "usr-lib-systemd",
        )
        self.catalog = {"compatibilityGroups": []}
        self.clock = VirtualClock()
        self.monotonic = self.clock.monotonic
        self.sleeper = self.clock.sleep
        self.events: list[tuple[Any, ...]] = []
        self.states: dict[str, tuple[str, str, str, str]] = {}
        self.fragments: dict[str, str] = {}
        self.dropins: dict[str, str] = {}
        self.reload_states: dict[str, str] = {}
        self.keep_restarting: set[str] = set()
        self.pointers: dict[str, str | None] = {}
        self.components: dict[str, dict[str, Any]] = {}
        self.items: list[dict[str, Any]] = []

    def _unit_uses_component_runner(self, _unit: Path, _component_id: str) -> bool:
        return True

    def _fsync_directory(self, _path: Path) -> None:
        return None

    def _active_native_pointer_identity(self, component_id: str) -> str | None:
        return self.pointers[component_id]

    def _activate_native(
        self, component_id: str, pointer_identity: str | None, *, expected_current: str | None
    ) -> None:
        assert self.pointers[component_id] == expected_current
        self.events.append(("pointer", component_id, pointer_identity))
        self.pointers[component_id] = pointer_identity

    def _clear_active_receipt(self, _component_id: str) -> None:
        return None

    def runner(self, command: list[str], **_kwargs: Any) -> Any:
        stdout = ""
        if command[:3] == [
            "systemctl",
            "show",
            "--property=ActiveState,SubState,MainPID,ControlPID",
        ]:
            unit = command[-1]
            values = self.states[unit]
            stdout = "\n".join(values)
            self.events.append(("state", unit, values))
        elif command[:2] == ["systemctl", "show"]:
            unit = command[-1]
            property_name = command[2].removeprefix("--property=")
            if property_name == "FragmentPath":
                stdout = self.fragments[unit]
            elif property_name == "DropInPaths":
                stdout = self.dropins[unit]
            elif property_name == "NeedDaemonReload":
                stdout = self.reload_states[unit]
            elif property_name == "ActiveState":
                stdout = self.states[unit][0]
            elif property_name == "MainPID":
                stdout = self.states[unit][2]
            else:
                raise AssertionError(f"Unexpected systemd property: {property_name}")
        elif command[:2] == ["systemctl", "daemon-reload"]:
            self.events.append(("daemon-reload",))
            for unit in self.reload_states:
                self.reload_states[unit] = "no"
        elif command[:2] == ["systemctl", "stop"]:
            unit = command[2]
            self.events.append(("stop", unit))
            if unit not in self.keep_restarting:
                self.states[unit] = ("inactive", "dead", "0", "0")
        else:
            raise AssertionError(f"Unexpected command: {command}")
        return type("Completed", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()


def _fixture(root: Path) -> tuple[UnitUpdater, list[dict[str, Any]]]:
    updater = UnitUpdater(root)
    for component_id in bootstrap.CORE_COMPONENT_IDS:
        unit = component_id + ".service"
        updater.components[component_id] = {
            "componentId": component_id,
            "systemdUnit": unit,
            "restart": {"unit": unit},
        }
        updater.pointers[component_id] = None
        updater.states[unit] = ("inactive", "dead", "0", "0")
        updater.fragments[unit] = str(updater.systemd_unit_dirs[0] / unit)
        updater.dropins[unit] = ""
        updater.reload_states[unit] = "no"

    items = []
    for component_id in bootstrap.CORE_COMPONENT_IDS:
        unit = updater.components[component_id]["systemdUnit"]
        version = "1.0.0"
        manifest_digest = _digest((component_id + " manifest").encode())
        pointer = version + "--" + manifest_digest.removeprefix("sha256:")
        release = updater.install_root / "components" / component_id / "releases" / pointer
        binary = ("signed " + component_id).encode()
        unit_bytes = (
            "[Service]\nExecStart=/usr/bin/cyrene component-run " + component_id + " --\n"
        ).encode()
        (release / "bin").mkdir(parents=True)
        (release / "systemd").mkdir()
        (release / "bin" / component_id).write_bytes(binary)
        (release / "bin" / component_id).chmod(0o755)
        unit_source = release / "systemd" / unit
        unit_source.write_bytes(unit_bytes)
        unit_source.chmod(0o644)
        items.append(
            {
                "componentId": component_id,
                "version": version,
                "manifestDigest": manifest_digest,
                "manifest": {
                    "artifact": {
                        "entrypoint": "bin/" + component_id,
                        "files": {
                            "bin/" + component_id: _digest(binary),
                            "systemd/" + unit: _digest(unit_bytes),
                        },
                    }
                },
            }
        )
    updater.items = items
    return updater, items


def test_pid0_auto_restart_restores_signed_unit_and_stops_before_pointer_cleanup(
    tmp_path: Path,
) -> None:
    """An orphaned restart loop is quiesced before active pointers and unit bytes are removed."""

    updater, items = _fixture(tmp_path)
    item = items[0]
    component_id = item["componentId"]
    unit = updater.components[component_id]["systemdUnit"]
    destination = updater.systemd_unit_dirs[0] / unit
    pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
    updater.pointers[component_id] = pointer
    updater.states[unit] = ("activating", "auto-restart", "0", "0")
    updater.reload_states[unit] = "yes"

    bootstrap._quiesce_hold_recovery_units(updater, items)
    assert updater.states[unit] == ("inactive", "dead", "0", "0")
    assert (
        destination.read_bytes()
        == (
            updater.install_root
            / "components"
            / component_id
            / "releases"
            / pointer
            / "systemd"
            / unit
        ).read_bytes()
    )
    assert updater.events.index(("daemon-reload",)) < updater.events.index(("stop", unit))
    assert updater.clock.now < bootstrap.CORE_UNIT_QUIESCE_WAIT_SECONDS

    bootstrap._remove_candidate_pointers_and_units(updater, items)
    pointer_event = updater.events.index(("pointer", component_id, None))
    stop_event = updater.events.index(("stop", unit))
    assert stop_event < pointer_event
    assert updater.pointers[component_id] is None
    assert not destination.exists()


@pytest.mark.parametrize(
    ("defect", "expected_error"),
    [
        ("foreign-fragment", "path or drop-ins"),
        ("drop-in", "path or drop-ins"),
        ("unknown-state", "stoppable PID0 state"),
        ("control-pid", "stoppable PID0 state"),
        ("reload-stays-needed", "exact signed unit"),
        ("stop-timeout", "did not reach inactive/dead"),
    ],
)
def test_unproven_or_unstoppable_candidate_keeps_pointer_and_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
    expected_error: str,
) -> None:
    """A mismatched unit, foreign control process, or timeout cannot authorize cleanup."""

    updater, items = _fixture(tmp_path)
    item = items[0]
    component_id = item["componentId"]
    unit = updater.components[component_id]["systemdUnit"]
    pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
    updater.pointers[component_id] = pointer
    updater.states[unit] = ("activating", "auto-restart", "0", "0")
    if defect == "foreign-fragment":
        updater.fragments[unit] = str(tmp_path / "foreign.service")
    elif defect == "drop-in":
        updater.dropins[unit] = str(tmp_path / "override.conf")
    elif defect == "unknown-state":
        updater.states[unit] = ("activating", "start", "0", "0")
    elif defect == "control-pid":
        updater.states[unit] = ("activating", "auto-restart", "0", "123")
    elif defect == "reload-stays-needed":
        updater.reload_states[unit] = "yes"
        monkeypatch.setattr(
            updater,
            "runner",
            lambda command, **kwargs: (
                type("Completed", (), {"returncode": 0, "stdout": "yes", "stderr": ""})()
                if command[:2] == ["systemctl", "show"]
                and command[2] == "--property=NeedDaemonReload"
                else UnitUpdater.runner(updater, command, **kwargs)
            ),
        )
    elif defect == "stop-timeout":
        updater.keep_restarting.add(unit)

    with pytest.raises(RuntimeError, match=expected_error):
        if defect in {"reload-stays-needed", "stop-timeout"}:
            bootstrap._quiesce_hold_recovery_units(updater, items)
        else:
            bootstrap._stop_candidate_services(updater, items)

    assert updater.pointers[component_id] == pointer
    assert not any(event[0] == "pointer" for event in updater.events)
    assert not any(event[0] == "end" for event in updater.events)
    if defect in {"foreign-fragment", "drop-in", "unknown-state", "control-pid"}:
        assert not any(event[0] == "stop" for event in updater.events)
    if defect == "stop-timeout":
        assert any(event[0] == "stop" for event in updater.events)
        assert updater.clock.now == pytest.approx(bootstrap.CORE_UNIT_QUIESCE_WAIT_SECONDS)


def test_foreign_live_mainpid_fails_exact_candidate_identity_before_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nonzero PID must pass the existing exact signed-entrypoint guard before stop."""

    updater, items = _fixture(tmp_path)
    item = items[0]
    unit = updater.components[item["componentId"]]["systemdUnit"]
    updater.states[unit] = ("active", "running", "123", "0")
    monkeypatch.setattr(bootstrap, "_candidate_executable", lambda *_args: tmp_path / "signed")
    monkeypatch.setattr(bootstrap, "_proc_executable_matches", lambda *_args, **_kwargs: False)

    with pytest.raises(RuntimeError, match="did not reach the exact staged release entrypoint"):
        bootstrap._stop_candidate_services(updater, items)

    assert not any(event[0] == "stop" for event in updater.events)
    assert not any(event[0] == "pointer" for event in updater.events)
    assert updater.clock.now == pytest.approx(bootstrap.CORE_EXEC_STARTUP_WAIT_SECONDS)


def test_held_recovery_unit_mismatch_preserves_pointers_and_does_not_end_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loaded unit from another path blocks the official held-plan recovery."""

    updater, plan, proc_root, _cleanup_calls = held_recovery._interrupted_apply(
        tmp_path, monkeypatch
    )
    before = dict(updater.pointers)
    unit = updater.components[bootstrap.LINUX_SYS_ADAPTER_COMPONENT_ID]["systemdUnit"]
    expected_path = str(updater.systemd_unit_dirs[0] / unit)
    events: list[tuple[str, ...]] = []
    original_runner = updater.runner

    def mismatched_unit_manager(command: list[str], **kwargs: Any) -> Any:
        if command[:3] == [
            "systemctl",
            "show",
            "--property=ActiveState,SubState,MainPID,ControlPID",
        ]:
            current_unit = command[-1]
            values = (
                ("activating", "auto-restart", "0", "0")
                if current_unit == unit
                else ("inactive", "dead", "0", "0")
            )
            return type(
                "Completed", (), {"returncode": 0, "stdout": "\n".join(values), "stderr": ""}
            )()
        if (
            command[:2] == ["systemctl", "show"]
            and command[2] == "--property=FragmentPath"
            and command[-1] == unit
        ):
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": expected_path + ".foreign", "stderr": ""},
            )()
        if command[:2] == ["systemctl", "stop"]:
            events.append(("stop", command[-1]))
        return original_runner(command, **kwargs)

    updater.runner = mismatched_unit_manager
    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    monkeypatch.setattr(bootstrap, "_assert_fresh", lambda *_args, **_kwargs: None)
    confirmation = {
        "mode": bootstrap.CORE_BOOTSTRAP_MODE,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
    }

    with pytest.raises(RuntimeError, match="recovery identity is not proven"):
        bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            confirmation,
            proc_root=proc_root,
        )

    journal = json.loads(bootstrap._journal_path(updater).read_text(encoding="utf-8"))
    assert journal["phase"] == "hold_required"
    assert updater.pointers == before
    assert events == []
    assert not any(event[0] == "end" for event in updater.events)
