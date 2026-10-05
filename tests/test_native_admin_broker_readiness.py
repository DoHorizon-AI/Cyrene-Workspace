"""Readiness tests for the signed native maintenance broker startup fence."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
NATIVE_PATH = ROOT / "tooling" / "acceptance" / "native-components-v2" / "native_acceptance.py"
ADMIN_PATH = NATIVE_PATH.with_name("admin_initialize.py")


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


native_acceptance = _load_module("native_acceptance", NATIVE_PATH)
admin_initialize = _load_module("native_admin_broker_readiness_test", ADMIN_PATH)


def _properties(unit: Path, *, state: str, pid: str, drop_ins: str = "") -> dict[str, str]:
    return {
        "LoadState": "loaded",
        "ActiveState": state,
        "FragmentPath": str(unit),
        "DropInPaths": drop_ins,
        "MainPID": pid,
    }


def _setup_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    property_reads: list[dict[str, str]],
    executable_reads: list[str],
) -> tuple[Path, list[list[str]]]:
    signed_release = tmp_path / "release"
    signed_release.mkdir()
    signed_unit = signed_release / admin_initialize.BOOTSTRAP_UNIT
    unit_bytes = b"[Service]\nExecStart=/usr/bin/cyrene component-run cyrene-runtime-maintenance\n"
    signed_unit.write_bytes(unit_bytes)
    signed_binary = signed_release / "cyrene-runtime-maintenance"
    signed_binary.write_bytes(b"signed broker ELF bytes")

    unit_directory = tmp_path / "systemd"
    unit_directory.mkdir()
    installed_unit = unit_directory / admin_initialize.BOOTSTRAP_UNIT
    installed_unit.write_bytes(unit_bytes)
    monkeypatch.setattr(admin_initialize, "SYSTEMD_UNIT_DIRECTORY", unit_directory)
    monkeypatch.setattr(
        admin_initialize,
        "_broker_unit_and_entrypoint",
        lambda _activation: (signed_unit, signed_binary),
    )
    properties = deque(property_reads)

    def read_properties(_unit: str) -> dict[str, str]:
        return properties.popleft() if len(properties) > 1 else properties[0]

    monkeypatch.setattr(admin_initialize, "_systemd_properties", read_properties)
    executables = deque(executable_reads)
    monkeypatch.setattr(
        admin_initialize.os,
        "readlink",
        lambda _path: executables.popleft() if len(executables) > 1 else executables[0],
    )
    commands: list[list[str]] = []

    def run_command(arguments: list[str], **_kwargs: Any) -> SimpleNamespace:
        commands.append(arguments)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        admin_initialize, "_run", lambda arguments, *, runner: run_command(arguments)
    )
    return signed_binary, commands


def test_active_wrapper_is_polled_until_same_pid_execs_signed_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not treat systemd active as ready while its wrapper is still running."""

    unit = tmp_path / "systemd" / admin_initialize.BOOTSTRAP_UNIT
    properties = [
        _properties(unit, state="inactive", pid="0"),
        _properties(unit, state="active", pid="356804"),
        _properties(unit, state="active", pid="356804"),
        _properties(unit, state="active", pid="356804"),
    ]
    wrapper = "/opt/cyrene/python/3.12.14/bin/python3.12"
    signed_binary, commands = _setup_probe(
        tmp_path,
        monkeypatch,
        properties,
        [
            wrapper,
            str(tmp_path / "release" / "cyrene-runtime-maintenance"),
            str(tmp_path / "release" / "cyrene-runtime-maintenance"),
        ],
    )

    result = admin_initialize._start_fresh_broker({}, lambda *_args, **_kwargs: None)

    assert result["mainPid"] == "356804"
    assert result["binaryPath"] == str(signed_binary)
    assert (
        result["binarySha256"] == "sha256:" + hashlib.sha256(b"signed broker ELF bytes").hexdigest()
    )
    assert commands == [["/usr/bin/systemctl", "start", admin_initialize.BOOTSTRAP_UNIT]]


def test_readiness_rechecks_pid_before_accepting_a_stable_signed_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed MainPID invalidates the first match and requires a fresh fence."""

    unit = tmp_path / "systemd" / admin_initialize.BOOTSTRAP_UNIT
    signed = str(tmp_path / "release" / "cyrene-runtime-maintenance")
    properties = [
        _properties(unit, state="inactive", pid="0"),
        _properties(unit, state="active", pid="356804"),
        _properties(unit, state="active", pid="356805"),
        _properties(unit, state="active", pid="356805"),
        _properties(unit, state="active", pid="356805"),
    ]
    signed_binary, _commands = _setup_probe(
        tmp_path, monkeypatch, properties, [signed, signed, signed, signed]
    )

    result = admin_initialize._start_fresh_broker({}, lambda *_args, **_kwargs: None)

    assert result["mainPid"] == "356805"
    assert result["binaryPath"] == str(signed_binary)


def test_drop_in_appearing_during_start_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recheck the unit and drop-in identity throughout the readiness wait."""

    unit = tmp_path / "systemd" / admin_initialize.BOOTSTRAP_UNIT
    properties = [
        _properties(unit, state="inactive", pid="0"),
        _properties(
            unit, state="active", pid="356804", drop_ins="/etc/systemd/system/override.conf"
        ),
    ]
    _setup_probe(tmp_path, monkeypatch, properties, [])

    with pytest.raises(
        admin_initialize.AdminInitializationError, match="unit identity or drop-ins"
    ):
        admin_initialize._start_fresh_broker({}, lambda *_args, **_kwargs: None)


def test_wrapper_that_never_execs_signed_binary_times_out_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong executable stays unready after the bounded polling interval."""

    unit = tmp_path / "systemd" / admin_initialize.BOOTSTRAP_UNIT
    properties = [
        _properties(unit, state="inactive", pid="0"),
        _properties(unit, state="active", pid="356804"),
    ]
    wrapper = "/opt/cyrene/python/3.12.14/bin/python3.12"
    _setup_probe(tmp_path, monkeypatch, properties, [wrapper])
    monkeypatch.setattr(admin_initialize, "BROKER_READINESS_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(admin_initialize, "BROKER_READINESS_POLL_SECONDS", 0.001)

    with pytest.raises(admin_initialize.AdminInitializationError, match="readiness timeout"):
        admin_initialize._start_fresh_broker({}, lambda *_args, **_kwargs: None)
