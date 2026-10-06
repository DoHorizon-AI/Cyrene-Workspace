"""Focused tests for the held first-Core adapter-socket recovery exception."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "native_first_core_held_recovery", ROOT / "packaging" / "native_core_bootstrap.py"
)
assert _SPEC is not None and _SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = bootstrap
_SPEC.loader.exec_module(bootstrap)
_CORE_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "native_core_bootstrap_held_recovery_fixtures",
    ROOT / "tests" / "test_native_core_bootstrap.py",
)
assert _CORE_FIXTURE_SPEC is not None and _CORE_FIXTURE_SPEC.loader is not None
core_fixtures = importlib.util.module_from_spec(_CORE_FIXTURE_SPEC)
sys.modules[_CORE_FIXTURE_SPEC.name] = core_fixtures
_CORE_FIXTURE_SPEC.loader.exec_module(core_fixtures)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class FakeUpdater:
    """Expose only the catalog and read-only observations needed by freshness checks."""

    def __init__(self, root: Path) -> None:
        self.install_root = root / "usr-lib-cyrene"
        self.core_runtime_root = root / "var-lib-cyrene" / "runtime"
        self.core_run_root = root / "run" / "cyrene"
        self.systemd_unit_dirs = (root / "etc-systemd", root / "lib-systemd")
        self.catalog = {"compatibilityGroups": []}
        self.components = {
            component_id: {
                "componentId": component_id,
                "systemdUnit": component_id + ".service",
            }
            for component_id in bootstrap.CORE_COMPONENT_IDS
        }
        self.pointers: dict[str, str | None] = dict.fromkeys(bootstrap.CORE_COMPONENT_IDS)
        self.pid = "378218"
        self.pid_sequence: list[str] = []
        self.active_state = "active"
        self.gpu_output = ""
        self.begin_response: dict[str, Any] = {
            "status": "MAINTENANCE_ACTIVE",
            "maintenance_origin": "CORE_BOOTSTRAP",
            "readiness_claimed": False,
            "held": True,
            "maintenance_token": "t" * 40,
            "gate_generation": 10,
            "blocker_codes": ["MAINTENANCE_ALREADY_BEGUN"],
        }
        self.broker_calls: list[tuple[str, dict[str, Any], str | None]] = []

    def _active_native_pointer_identity(self, component_id: str) -> str | None:
        return self.pointers[component_id]

    def _broker_request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        self.broker_calls.append((method, params, request_id))
        return self.begin_response

    def runner(self, command: list[str], **_kwargs: Any) -> Any:
        if command[:2] == ["systemctl", "show"]:
            property_name = command[2].split("=", 1)[1]
            if property_name == "ActiveState":
                stdout = self.active_state
            elif self.pid_sequence:
                stdout = (
                    self.pid_sequence.pop(0) if len(self.pid_sequence) > 1 else self.pid_sequence[0]
                )
            else:
                stdout = self.pid
        elif command and command[0] == "nvidia-smi":
            stdout = self.gpu_output
        else:
            stdout = ""
        return SimpleNamespace(returncode=0, stdout=stdout)


@pytest.fixture
def held_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeUpdater, dict[str, Any], Path, Path, socket.socket]:
    """Create an isolated root-owned fixture with one exact active adapter process."""

    original_lstat = Path.lstat
    fixture_root = tmp_path.resolve()

    def root_owned_lstat(path: Path) -> os.stat_result:
        metadata = original_lstat(path)
        if path.absolute().is_relative_to(fixture_root):
            fields = list(metadata)
            fields[4] = 0
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(Path, "lstat", root_owned_lstat)
    updater = FakeUpdater(fixture_root)
    monkeypatch.setattr(bootstrap, "CORE_RUN_ROOT", updater.core_run_root)
    updater.core_runtime_root.mkdir(parents=True)
    updater.core_run_root.mkdir(mode=0o750, parents=True)
    updater.core_run_root.chmod(0o750)

    component_id = bootstrap.LINUX_SYS_ADAPTER_COMPONENT_ID
    updater.components[component_id]["systemdUnit"] = bootstrap.LINUX_SYS_ADAPTER_UNIT
    payload = b"signed linux system adapter fixture\n"
    manifest_digest = _digest(b"adapter manifest")
    version = "1.0.0"
    entrypoint = "bin/cyrene-linux-sys-adapter"
    release = (
        updater.install_root
        / "components"
        / component_id
        / "releases"
        / f"{version}--{manifest_digest.removeprefix('sha256:')}"
    )
    executable = release / entrypoint
    executable.parent.mkdir(parents=True)
    executable.write_bytes(payload)
    executable.chmod(0o755)
    item = {
        "componentId": component_id,
        "version": version,
        "manifestDigest": manifest_digest,
        "manifest": {
            "artifact": {"entrypoint": entrypoint, "files": {entrypoint: _digest(payload)}}
        },
    }
    updater.pointers[component_id] = f"{version}--{manifest_digest.removeprefix('sha256:')}"

    socket_path = updater.core_run_root / bootstrap.LINUX_SYS_ADAPTER_SOCKET
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o660)

    proc_root = fixture_root / "proc"
    proc_exe = proc_root / updater.pid / "exe"
    proc_exe.parent.mkdir(parents=True)
    proc_exe.symlink_to(executable)
    monkeypatch.setattr(
        bootstrap,
        "_core_process_snapshot",
        lambda *_args, **_kwargs: [(component_id, str(executable), updater.pid)],
    )
    monkeypatch.setattr(
        bootstrap,
        "_unix_socket_peer_credentials",
        lambda _path: (int(updater.pid), 0, socket_path.lstat().st_gid),
    )
    return updater, item, executable, proc_root, listener


def _held_transaction(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": "hold_required",
        "requestId": plan["requestId"],
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "maintenanceToken": "t" * 40,
        "maintenanceGateGeneration": 10,
        "beginRequest": bootstrap._core_bootstrap_begin_request(plan),
    }


def _plan(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "requestId": "request-1",
        "planId": "plan-1",
        "planDigest": _digest(b"first core plan"),
        "gateGeneration": 9,
        "catalogGeneration": 4,
        "activitySources": ["source-a"],
        "componentArtifactDigests": {item["componentId"]: _digest(b"adapter artifact")},
    }


def _interrupted_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, dict[str, Any], Path, list[str]]:
    """Create a real held journal through apply's failure path with isolated host calls."""

    updater = core_fixtures.FakeUpdater(tmp_path)
    plan = core_fixtures._check_plan(updater, tmp_path)
    core_fixtures.bootstrap.stage(updater, plan["planId"], plan["planDigest"])
    cleanup_calls: list[str] = []
    start_count = 0

    def fail_first_start(_updater: Any, _components: list[dict[str, Any]]) -> None:
        nonlocal start_count
        start_count += 1
        if start_count == 1:
            raise RuntimeError("simulated service startup interruption")

    monkeypatch.setattr(bootstrap, "_is_root", lambda: True)
    monkeypatch.setattr(bootstrap, "_start_core_components", fail_first_start)
    monkeypatch.setattr(
        bootstrap,
        "_stop_candidate_services",
        lambda *_args: cleanup_calls.append("stop"),
    )
    monkeypatch.setattr(
        bootstrap,
        "_remove_candidate_pointers_and_units",
        lambda *_args: cleanup_calls.append("remove"),
    )
    proc_root = core_fixtures._fake_proc(tmp_path / "proc-initial")
    confirmation = {
        "mode": bootstrap.CORE_BOOTSTRAP_MODE,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
    }
    with pytest.raises(RuntimeError, match="durable admission hold remains closed"):
        bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            confirmation,
            proc_root=proc_root,
        )
    journal_path = bootstrap._journal_path(updater)
    journal = bootstrap._read_private_json(journal_path)
    assert journal is not None and journal["phase"] == "hold_required"
    return updater, plan, proc_root, cleanup_calls


def _add_active_adapter_socket(
    updater: Any,
    plan: dict[str, Any],
    proc_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, socket.socket]:
    """Model the exact leftover staged Adapter unit and its bound Unix socket."""

    journal_path = bootstrap._journal_path(updater)
    journal = bootstrap._read_private_json(journal_path)
    assert journal is not None
    stage_path = Path(updater.state_root) / "staged" / plan["basePlanId"] / "stage.json"
    stage = json.loads(stage_path.read_text(encoding="utf-8"))
    items = stage["components"]
    item = next(
        entry for entry in items if entry["componentId"] == bootstrap.LINUX_SYS_ADAPTER_COMPONENT_ID
    )
    entrypoint = item["manifest"]["artifact"]["entrypoint"]
    payload = b"signed held recovery adapter image\n"
    item["manifest"]["artifact"]["files"] = {entrypoint: _digest(payload)}
    pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
    executable = (
        Path(updater.install_root)
        / "components"
        / item["componentId"]
        / "releases"
        / pointer
        / entrypoint
    )
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(payload)
    executable.chmod(0o755)
    stage_path.write_text(json.dumps(stage), encoding="utf-8")
    stage_path.chmod(0o600)
    journal["components"] = items
    bootstrap._write_private_json(updater, journal_path, journal)

    run_root = tmp_path / "run" / "cyrene"
    run_root.mkdir(mode=0o750, parents=True)
    run_root.chmod(0o750)
    updater.core_run_root = run_root
    monkeypatch.setattr(bootstrap, "CORE_RUN_ROOT", run_root)
    socket_path = run_root / bootstrap.LINUX_SYS_ADAPTER_SOCKET
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o660)

    original_lstat = Path.lstat
    fixture_root = tmp_path.resolve()

    def root_owned_lstat(path: Path) -> os.stat_result:
        metadata = original_lstat(path)
        if path.absolute().is_relative_to(fixture_root) and (
            path.absolute().is_relative_to(run_root) or path.absolute() == executable.absolute()
        ):
            fields = list(metadata)
            fields[4] = 0
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(Path, "lstat", root_owned_lstat)

    pid = "378218"
    updater.unit_pids[bootstrap.LINUX_SYS_ADAPTER_UNIT] = pid
    process = proc_root / pid
    process.mkdir()
    (process / "stat").write_text(f"{pid} (cyrene-linux-sys-adapter) S 1", encoding="ascii")
    (process / "cmdline").write_bytes(b"cyrene-linux-sys-adapter\0")
    (process / "exe").symlink_to(executable)
    monkeypatch.setattr(
        bootstrap,
        "_unix_socket_peer_credentials",
        lambda _path: (int(pid), 0, socket_path.lstat().st_gid),
    )
    return executable, listener


def test_exact_held_adapter_socket_is_allowed_during_full_freshness_check(
    held_adapter: tuple[FakeUpdater, dict[str, Any], Path, Path, socket.socket],
) -> None:
    updater, item, _executable, proc_root, listener = held_adapter
    plan = _plan(item)
    transaction = _held_transaction(plan)
    updater.pointers[item["componentId"]] = (
        f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
    )

    bootstrap._verify_held_core_bootstrap(updater, plan, transaction)
    proof = bootstrap._verify_held_linux_sys_adapter_socket(updater, [item], proc_root=proc_root)
    result = bootstrap._assert_fresh(
        updater,
        proc_root=proc_root,
        planned_components=[item],
        held_adapter_socket_proof=proof,
    )

    assert result is None
    assert updater.broker_calls == [
        ("BeginCoreBootstrap", transaction["beginRequest"], plan["requestId"])
    ]
    listener.close()


def test_adapter_socket_still_blocks_without_the_narrow_recovery_proof(
    held_adapter: tuple[FakeUpdater, dict[str, Any], Path, Path, socket.socket],
) -> None:
    updater, item, _executable, proc_root, listener = held_adapter
    with pytest.raises(ValueError, match="linux-sys-adapter.sock"):
        bootstrap._assert_fresh(
            updater,
            proc_root=proc_root,
            planned_components=[item],
        )
    assert updater.broker_calls == []
    listener.close()


def test_wrong_peer_pid_rejects_the_adapter_socket_recovery(
    held_adapter: tuple[FakeUpdater, dict[str, Any], Path, Path, socket.socket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater, item, _executable, proc_root, listener = held_adapter
    monkeypatch.setattr(bootstrap, "_unix_socket_peer_credentials", lambda _path: (1, 0, 999))

    with pytest.raises(RuntimeError, match="peer differs"):
        bootstrap._verify_held_linux_sys_adapter_socket(updater, [item], proc_root=proc_root)
    listener.close()


def test_socket_inode_change_during_peer_probe_is_rejected(
    held_adapter: tuple[FakeUpdater, dict[str, Any], Path, Path, socket.socket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater, item, _executable, proc_root, listener = held_adapter
    socket_path = updater.core_run_root / bootstrap.LINUX_SYS_ADAPTER_SOCKET
    replacement: list[socket.socket] = []

    def replace_socket(_path: Path) -> tuple[int, int, int]:
        socket_path.unlink()
        new_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        new_listener.bind(str(socket_path))
        socket_path.chmod(0o660)
        replacement.append(new_listener)
        listener.close()
        return int(updater.pid), 0, socket_path.lstat().st_gid

    monkeypatch.setattr(bootstrap, "_unix_socket_peer_credentials", replace_socket)
    with pytest.raises(RuntimeError, match="identity changed"):
        bootstrap._verify_held_linux_sys_adapter_socket(updater, [item], proc_root=proc_root)
    for opened in replacement:
        opened.close()


def test_parent_identity_change_during_peer_probe_is_rejected(
    held_adapter: tuple[FakeUpdater, dict[str, Any], Path, Path, socket.socket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater, item, _executable, proc_root, listener = held_adapter
    original_identity = bootstrap._path_identity
    parent_reads = 0

    def change_parent_identity(path: Path, *, kind: str) -> tuple[int, int, int, int, int]:
        nonlocal parent_reads
        identity = original_identity(path, kind=kind)
        if kind == "directory":
            parent_reads += 1
            if parent_reads == 2:
                return (identity[0], identity[1] + 1, *identity[2:])
        return identity

    monkeypatch.setattr(bootstrap, "_path_identity", change_parent_identity)
    with pytest.raises(RuntimeError, match="identity changed"):
        bootstrap._verify_held_linux_sys_adapter_socket(updater, [item], proc_root=proc_root)
    listener.close()


def test_changed_main_pid_during_peer_probe_is_rejected(
    held_adapter: tuple[FakeUpdater, dict[str, Any], Path, Path, socket.socket],
) -> None:
    updater, item, _executable, proc_root, listener = held_adapter
    updater.pid_sequence = ["378218", "378219"]

    with pytest.raises(RuntimeError, match="identity changed"):
        bootstrap._verify_held_linux_sys_adapter_socket(updater, [item], proc_root=proc_root)
    listener.close()


def test_failed_adapter_unit_never_qualifies_for_recovery(
    held_adapter: tuple[FakeUpdater, dict[str, Any], Path, Path, socket.socket],
) -> None:
    updater, item, _executable, proc_root, listener = held_adapter
    updater.active_state = "failed"

    with pytest.raises(RuntimeError, match="not the exact active unit process"):
        bootstrap._verify_held_linux_sys_adapter_socket(updater, [item], proc_root=proc_root)
    listener.close()


@pytest.mark.parametrize("blocked_path", ["kernel.sock", "workers"])
def test_other_kernel_runtime_paths_still_block_with_adapter_proof(
    held_adapter: tuple[FakeUpdater, dict[str, Any], Path, Path, socket.socket],
    blocked_path: str,
) -> None:
    updater, item, _executable, proc_root, listener = held_adapter
    proof = bootstrap._verify_held_linux_sys_adapter_socket(updater, [item], proc_root=proc_root)
    path = updater.core_run_root / blocked_path
    if blocked_path == "workers":
        path.mkdir()
    else:
        path.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match=blocked_path):
        bootstrap._assert_fresh(
            updater,
            proc_root=proc_root,
            planned_components=[item],
            held_adapter_socket_proof=proof,
        )
    listener.close()


def test_begin_replay_rejects_a_changed_hold_token(
    held_adapter: tuple[FakeUpdater, dict[str, Any], Path, Path, socket.socket],
) -> None:
    updater, item, _executable, _proc_root, listener = held_adapter
    plan = _plan(item)
    transaction = _held_transaction(plan)
    updater.begin_response = {**updater.begin_response, "maintenance_token": "changed"}

    with pytest.raises(RuntimeError, match="same active first-Core maintenance hold"):
        bootstrap._verify_held_core_bootstrap(updater, plan, transaction)
    assert updater.broker_calls[0] == (
        "BeginCoreBootstrap",
        transaction["beginRequest"],
        plan["requestId"],
    )
    listener.close()


def test_unix_socket_reader_uses_linux_peer_credentials_without_sending_a_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected_path = tmp_path / "linux-sys-adapter.sock"
    packed = struct.pack("3i", 77, 0, 999)

    class FakeSocket:
        def __init__(self, family: int, kind: int) -> None:
            assert family == socket.AF_UNIX
            assert kind == socket.SOCK_STREAM
            self.options: tuple[int, int, int] | None = None
            self.connected: str | None = None
            self.sent = False

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def settimeout(self, value: float) -> None:
            assert value == 1.0

        def connect(self, path: str) -> None:
            self.connected = path

        def getsockopt(self, level: int, option: int, size: int) -> bytes:
            self.options = (level, option, size)
            return packed

    fake_sockets: list[FakeSocket] = []

    def make_socket(family: int, kind: int) -> FakeSocket:
        instance = FakeSocket(family, kind)
        fake_sockets.append(instance)
        return instance

    monkeypatch.setattr(bootstrap.socket, "socket", make_socket)

    assert bootstrap._unix_socket_peer_credentials(expected_path) == (77, 0, 999)
    assert fake_sockets[0].connected == str(expected_path)
    assert fake_sockets[0].options == (
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )
    assert fake_sockets[0].sent is False


def test_apply_resumes_hold_required_transaction_when_cleanup_removed_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater, plan, proc_root, cleanup_calls = _interrupted_apply(tmp_path, monkeypatch)
    cleanup_calls.clear()
    monkeypatch.setattr(bootstrap, "_verify_live_core_cohort", lambda *_args: None)
    monkeypatch.setattr(bootstrap, "_require_core_ready", lambda *_args: None)
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
        proc_root=proc_root,
    )

    assert result["status"] == "installed"
    assert (
        len([call for call in updater.events if call[:2] == ("broker", "BeginCoreBootstrap")]) == 2
    )
    assert len([event for event in updater.events if event[0] == "end"]) == 1
    assert cleanup_calls == []


def test_apply_recovers_exact_owned_adapter_socket_under_the_existing_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater, plan, proc_root, cleanup_calls = _interrupted_apply(tmp_path, monkeypatch)
    _executable, listener = _add_active_adapter_socket(
        updater, plan, proc_root, tmp_path, monkeypatch
    )
    cleanup_calls.clear()
    monkeypatch.setattr(bootstrap, "_verify_live_core_cohort", lambda *_args: None)
    monkeypatch.setattr(bootstrap, "_require_core_ready", lambda *_args: None)
    confirmation = {
        "mode": bootstrap.CORE_BOOTSTRAP_MODE,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
    }

    try:
        result = bootstrap.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            confirmation,
            proc_root=proc_root,
        )
    finally:
        listener.close()

    assert result["status"] == "installed"
    assert (
        len([call for call in updater.events if call[:2] == ("broker", "BeginCoreBootstrap")]) == 2
    )
    assert len([event for event in updater.events if event[0] == "end"]) == 1
    assert cleanup_calls == []


@pytest.mark.parametrize("failure", ["changed-hold", "wrong-peer"])
def test_apply_recovery_failure_keeps_hold_without_stopping_or_ending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    updater, plan, proc_root, cleanup_calls = _interrupted_apply(tmp_path, monkeypatch)
    _executable, listener = _add_active_adapter_socket(
        updater, plan, proc_root, tmp_path, monkeypatch
    )
    cleanup_calls.clear()
    if failure == "changed-hold":
        original_broker_request = updater._broker_request

        def changed_hold_response(
            method: str, params: dict[str, Any], **kwargs: Any
        ) -> dict[str, Any]:
            response = original_broker_request(method, params, **kwargs)
            return (
                {**response, "maintenance_token": "different"}
                if method == "BeginCoreBootstrap"
                else response
            )

        updater._broker_request = changed_hold_response
    else:
        monkeypatch.setattr(
            bootstrap,
            "_unix_socket_peer_credentials",
            lambda _path: (1, 0, 999),
        )
    confirmation = {
        "mode": bootstrap.CORE_BOOTSTRAP_MODE,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
    }

    try:
        with pytest.raises(RuntimeError, match="recovery identity is not proven"):
            bootstrap.apply(
                updater,
                plan["planId"],
                plan["planDigest"],
                confirmation,
                proc_root=proc_root,
            )
    finally:
        listener.close()

    journal = bootstrap._read_private_json(bootstrap._journal_path(updater))
    assert journal is not None and journal["phase"] == "hold_required"
    assert cleanup_calls == []
    assert not any(event[0] == "end" for event in updater.events)
