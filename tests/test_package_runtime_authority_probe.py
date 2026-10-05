"""Exercise the authenticated local Package Runtime authority probe."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import struct
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "packaging/native_package_runtime_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("package_runtime_authority_probe_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)


def _catalog(generation: int = 9) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generation": generation,
        "sources": [
            {
                "source_id": "cyrene-yield",
                "uid": os.geteuid(),
                "gid": os.getegid(),
                "source_token_sha256": "a" * 64,
                "binding_scopes": [],
            }
        ],
    }


def _serve_once(
    path: Path, result: dict[str, Any]
) -> tuple[threading.Thread, list[dict[str, Any]], list[tuple[int, int, int]]]:
    requests: list[dict[str, Any]] = []
    peer_credentials: list[tuple[int, int, int]] = []
    path.parent.chmod(0o750)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o660)
    listener.listen(1)

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                peer_credentials.append(
                    struct.unpack(
                        "3i",
                        connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
                    )
                )
                content = b""
                while b"\n" not in content:
                    chunk = connection.recv(65536)
                    if not chunk:
                        return
                    content += chunk
                requests.append(json.loads(content.split(b"\n", 1)[0]))
                response = {
                    "request_id": requests[0]["request_id"],
                    "ok": True,
                    "result": result,
                }
                connection.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread, requests, peer_credentials


def _configure_runtime_socket_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "_runtime_user_id", os.geteuid)
    monkeypatch.setattr(bootstrap, "_runtime_group_id", os.getegid)


def test_authority_probe_authenticates_yield_and_checks_exact_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = tmp_path / "control.sock"
    result = {
        "authority": "platform_package_runtime",
        "protocol_version": "cy-package-runtime.control.v1",
        "catalog_generation": 9,
        "capabilities": ["cy-package-runtime.binding-operation-admission.v1"],
    }
    server, requests, peer_credentials = _serve_once(socket_path, result)
    _configure_runtime_socket_identity(monkeypatch)
    monkeypatch.setattr(
        bootstrap, "_read_source_token", lambda _path, digest: "private-source-token"
    )

    proof = bootstrap.probe_runtime_authority(
        _catalog(), expected_catalog_generation=9, socket_path=socket_path
    )
    server.join(timeout=2)

    assert not server.is_alive()
    assert proof["catalog_generation"] == 9
    assert peer_credentials[0][1:] == (os.geteuid(), os.getegid())
    assert requests[0]["operation"] == "authority"
    assert requests[0]["catalog_generation"] == 9
    assert requests[0]["auth"] == {
        "source_id": "cyrene-yield",
        "source_token": "private-source-token",
    }
    assert "private-source-token" not in json.dumps(proof)


@pytest.mark.parametrize(
    "field,value",
    [
        ("authority", "other_runtime"),
        ("protocol_version", "cy-package-runtime.control.v0"),
        ("catalog_generation", 8),
        ("capabilities", ["cyrene.runtime-maintenance.binding-operations.v1"]),
        ("capabilities", []),
    ],
)
def test_authority_probe_rejects_unknown_daemon_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    socket_path = tmp_path / "control.sock"
    result = {
        "authority": "platform_package_runtime",
        "protocol_version": "cy-package-runtime.control.v1",
        "catalog_generation": 9,
        "capabilities": ["cy-package-runtime.binding-operation-admission.v1"],
    }
    result[field] = value
    server, _requests, peer_credentials = _serve_once(socket_path, result)
    _configure_runtime_socket_identity(monkeypatch)
    monkeypatch.setattr(
        bootstrap, "_read_source_token", lambda _path, _digest: "private-source-token"
    )

    with pytest.raises(bootstrap.PackageRuntimeBootstrapError, match="identity is unknown"):
        bootstrap.probe_runtime_authority(
            _catalog(), expected_catalog_generation=9, socket_path=socket_path
        )
    server.join(timeout=2)
    assert peer_credentials[0][1:] == (os.geteuid(), os.getegid())


def test_authority_probe_rejects_stale_installed_catalog_before_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "_read_source_token", lambda *_args: pytest.fail("read token"))
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError, match="generation is stale"):
        bootstrap.probe_runtime_authority(
            _catalog(8), expected_catalog_generation=9, socket_path=tmp_path / "absent.sock"
        )


def test_authority_probe_rejects_catalog_peer_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_catalog = _catalog()
    source_catalog["sources"][0]["uid"] += 1
    monkeypatch.setattr(bootstrap, "_read_source_token", lambda *_args: "private-source-token")
    _configure_runtime_socket_identity(monkeypatch)
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError, match="service identity"):
        bootstrap.probe_runtime_authority(
            source_catalog,
            expected_catalog_generation=9,
            socket_path=tmp_path / "absent.sock",
        )


def test_source_token_reader_requires_root_private_exact_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "cyrene-yield.token"
    token = b"private-source-token"
    token_path.write_bytes(token + b"\n")
    token_path.chmod(0o400)
    real_fstat = os.fstat

    def root_owned_fstat(fd: int) -> os.stat_result:
        fields = list(real_fstat(fd))
        fields[4] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(bootstrap.os, "fstat", root_owned_fstat)
    assert (
        bootstrap._read_source_token(token_path, hashlib.sha256(token).hexdigest())
        == token.decode()
    )
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError, match="digest differs"):
        bootstrap._read_source_token(token_path, "0" * 64)
    token_path.unlink()
    token_path.symlink_to(tmp_path / "missing")
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError, match="unavailable"):
        bootstrap._read_source_token(token_path, hashlib.sha256(token).hexdigest())


def test_root_probe_drops_to_catalog_peer_without_putting_token_in_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_changes: list[tuple[str, Any]] = []
    command_seen: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        command_seen.append(command)
        assert kwargs["input"] == b'{"source_token":"private-token"}\n'
        kwargs["preexec_fn"]()
        return type("Completed", (), {"returncode": 0, "stdout": b"{}\n"})()

    monkeypatch.setattr(bootstrap, "_effective_uid", lambda: 0)
    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(
        bootstrap.os, "setgroups", lambda groups: identity_changes.append(("groups", groups))
    )
    monkeypatch.setattr(bootstrap.os, "setgid", lambda gid: identity_changes.append(("gid", gid)))
    monkeypatch.setattr(bootstrap.os, "setuid", lambda uid: identity_changes.append(("uid", uid)))

    assert (
        bootstrap._run_authority_probe_as_source(
            b'{"source_token":"private-token"}\n',
            socket_path=Path("/run/cyrene-package-runtime/control.sock"),
            uid=1001,
            gid=1002,
            timeout=5,
        )
        == b"{}\n"
    )
    assert identity_changes == [("groups", []), ("gid", 1002), ("uid", 1001)]
    assert all("private-token" not in argument for argument in command_seen[0])
