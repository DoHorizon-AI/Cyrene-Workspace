"""Regression coverage for bounded first-Core executable startup verification."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "native_first_core_startup_wait_test", ROOT / "packaging" / "native_core_bootstrap.py"
)
assert spec is not None and spec.loader is not None
bootstrap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bootstrap
spec.loader.exec_module(bootstrap)


class VirtualClock:
    """Advance deterministically whenever the bootstrap sleeps."""

    def __init__(self, on_sleep: Callable[[float], None] | None = None) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.on_sleep = on_sleep

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration
        if self.on_sleep is not None:
            self.on_sleep(self.now)


class StartupUpdater:
    """Model systemd observations and record candidate service operations."""

    def __init__(self, component_ids: tuple[str, ...]) -> None:
        self.components = {
            component_id: {"systemdUnit": component_id + ".service"}
            for component_id in component_ids
        }
        self.catalog = {"compatibilityGroups": []}
        self.unit_pids = {
            component["systemdUnit"]: str(100 + index)
            for index, component in enumerate(self.components.values(), start=1)
        }
        self.active_states = {unit: "active" for unit in self.unit_pids}
        self.pid_sequences: dict[str, list[str]] = {}
        self.events: list[tuple[str, ...]] = []
        self.on_runner: Callable[[list[str], str], None] | None = None

    def runner(self, command: list[str], **_kwargs: Any) -> Any:
        if command[:2] != ["systemctl", "show"]:
            return _completed("")
        if len(command) > 2 and command[2] == (
            "--property=ActiveState,SubState,MainPID,ControlPID"
        ):
            unit = command[-1]
            active = self.active_states[unit]
            substate = "running" if active == "active" else "dead"
            pid = self.unit_pids[unit] if active == "active" else "0"
            values = f"ActiveState={active}\nSubState={substate}\nMainPID={pid}\nControlPID=0"
            self.events.append(("show", "unit-state", unit, values))
            return _completed(values)
        property_name = next(
            argument.removeprefix("--property=")
            for argument in command
            if argument.startswith("--property=")
        )
        unit = command[-1]
        if property_name == "ActiveState":
            value = self.active_states[unit]
        else:
            sequence = self.pid_sequences.get(unit)
            value = (
                sequence.pop(0)
                if sequence and len(sequence) > 1
                else (sequence[0] if sequence else self.unit_pids[unit])
            )
        self.events.append(("show", property_name, unit, value))
        if self.on_runner is not None:
            self.on_runner(command, value)
        return _completed(value)

    def _run_systemctl(self, operation: str, unit: str) -> None:
        self.events.append((operation, unit))
        if operation == "stop":
            self.active_states[unit] = "inactive"
            self.unit_pids[unit] = "0"

    def _wait_unit_active(self, unit: str) -> None:
        self.events.append(("wait-active", unit))


def _completed(stdout: str) -> Any:
    return type("Completed", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()


def _startup_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    wrong_executable: bool = False,
    on_sleep: Callable[[float], None] | None = None,
) -> tuple[StartupUpdater, list[dict[str, Any]], dict[str, Path], Path, VirtualClock]:
    """Create staged-path and proc fixtures without using host processes."""

    component_ids = bootstrap.CORE_COMPONENT_IDS
    updater = StartupUpdater(component_ids)
    expected_paths: dict[str, Path] = {}
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    items = []
    launcher = tmp_path / "cyrene-component-runner"
    launcher.write_bytes(b"trusted runner fixture")
    unknown = tmp_path / "unknown-executable"
    unknown.write_bytes(b"unowned process fixture")

    for index, component_id in enumerate(component_ids, start=1):
        expected = tmp_path / f"signed-{component_id}"
        expected.write_bytes(f"signed ELF {component_id}".encode())
        expected_paths[component_id] = expected.resolve(strict=True)
        pid = updater.unit_pids[updater.components[component_id]["systemdUnit"]]
        process = proc_root / pid
        process.mkdir()
        executable = process / "exe"
        executable.symlink_to(unknown if wrong_executable else expected)
        item = {
            "componentId": component_id,
            "version": "1.0.0",
            "manifestDigest": "sha256:" + str(index) * 64,
            "manifest": {"artifact": {"entrypoint": "bin/entrypoint"}},
        }
        items.append(item)

    monkeypatch.setattr(bootstrap, "PROC_ROOT", proc_root)
    monkeypatch.setattr(
        bootstrap,
        "_candidate_executable",
        lambda _updater, item: expected_paths[item["componentId"]],
    )
    clock = VirtualClock(on_sleep)
    updater.monotonic = clock.monotonic
    updater.sleeper = clock.sleep
    return updater, items, expected_paths, launcher, clock


def _replace_link_at(path: Path, target: Path) -> None:
    path.unlink()
    path.symlink_to(target)


def test_start_waits_for_runner_to_exec_exact_signed_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not start the next Core component until the current PID execs its ELF."""

    updater, items, expected, launcher, clock = _startup_fixture(
        tmp_path, monkeypatch, wrong_executable=True
    )
    first_unit = updater.components[items[0]["componentId"]]["systemdUnit"]
    first_pid = updater.unit_pids[first_unit]
    first_exe = bootstrap.PROC_ROOT / first_pid / "exe"
    _replace_link_at(first_exe, launcher)
    for item in items[1:]:
        component_id = item["componentId"]
        unit = updater.components[component_id]["systemdUnit"]
        _replace_link_at(
            bootstrap.PROC_ROOT / updater.unit_pids[unit] / "exe", expected[component_id]
        )
    updater.events.clear()

    def exec_after_startup(now: float) -> None:
        if now >= 0.2 and first_exe.resolve(strict=True) != expected[items[0]["componentId"]]:
            _replace_link_at(first_exe, expected[items[0]["componentId"]])
            updater.events.append(("exec", first_unit))

    clock.on_sleep = exec_after_startup
    bootstrap._start_core_components(updater, items)

    starts = [event for event in updater.events if event[0] == "start"]
    exec_index = updater.events.index(("exec", first_unit))
    second_start_index = updater.events.index(starts[1])
    assert len(starts) == len(bootstrap.CORE_COMPONENT_IDS)
    assert exec_index < second_start_index
    assert 0.2 <= clock.now < bootstrap.CORE_EXEC_STARTUP_WAIT_SECONDS


def test_wrong_executable_times_out_within_fixed_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that never reaches the candidate ELF fails at the fixed deadline."""

    updater, items, _expected, _launcher, clock = _startup_fixture(
        tmp_path, monkeypatch, wrong_executable=True
    )
    unit = updater.components[items[0]["componentId"]]["systemdUnit"]

    with pytest.raises(RuntimeError, match="did not reach the exact staged release entrypoint"):
        bootstrap._verify_candidate_pid(updater, items[0], updater.unit_pids[unit])

    assert clock.now == pytest.approx(bootstrap.CORE_EXEC_STARTUP_WAIT_SECONDS)
    assert len(clock.sleeps) <= 101


def test_slow_systemd_query_cannot_succeed_after_startup_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Include unit-property calls in the same bounded identity deadline."""

    updater, items, _expected, _launcher, clock = _startup_fixture(tmp_path, monkeypatch)
    unit = updater.components[items[0]["componentId"]]["systemdUnit"]

    def finish_after_deadline(_command: list[str], _value: str) -> None:
        clock.now = bootstrap.CORE_EXEC_STARTUP_WAIT_SECONDS + 0.01

    updater.on_runner = finish_after_deadline
    with pytest.raises(RuntimeError, match="did not reach the exact staged release entrypoint"):
        bootstrap._verify_candidate_pid(updater, items[0], updater.unit_pids[unit])


def test_pid_change_during_startup_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed MainPID cannot inherit the candidate's startup authorization."""

    updater, items, _expected, _launcher, _clock = _startup_fixture(tmp_path, monkeypatch)
    unit = updater.components[items[0]["componentId"]]["systemdUnit"]
    original = updater.unit_pids[unit]
    updater.pid_sequences[unit] = [original, str(int(original) + 100)]

    with pytest.raises(RuntimeError, match="MainPID changed"):
        bootstrap._verify_started_processes(updater, items[:1])


def test_failed_first_unit_prevents_starting_later_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unit that leaves active state stops the ordered startup immediately."""

    updater, items, _expected, _launcher, _clock = _startup_fixture(tmp_path, monkeypatch)
    first_unit = updater.components[items[0]["componentId"]]["systemdUnit"]
    updater.active_states[first_unit] = "failed"

    with pytest.raises(RuntimeError, match="left active state"):
        bootstrap._start_core_components(updater, items)

    assert [event[1] for event in updater.events if event[0] == "start"] == [first_unit]


def test_reverse_cleanup_never_stops_a_pid_with_unknown_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverse cleanup preserves the hold when the candidate PID is not proven."""

    updater, items, _expected, _launcher, clock = _startup_fixture(
        tmp_path, monkeypatch, wrong_executable=True
    )
    reverse_first = updater.components[items[-1]["componentId"]]["systemdUnit"]

    with pytest.raises(RuntimeError, match="did not reach the exact staged release entrypoint"):
        bootstrap._stop_candidate_services(updater, items)

    assert not any(event[0] == "stop" for event in updater.events)
    assert updater.events[0][2] == reverse_first
    assert clock.now == pytest.approx(bootstrap.CORE_EXEC_STARTUP_WAIT_SECONDS)
