"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 plugin_grpc_health_probe.py                                          │
│  Module: tooling.deploy.teams_cloud.plugin_grpc_health_probe             │
│  Role: Verify one DirectPluginRuntime over mutually authenticated TLS.   │
│                                                                          │
│  模块职责：通过双向 TLS 核验一个 DirectPluginRuntime 的健康状态。       │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import grpc
from cyrene_plugin_runtime._generated import direct_plugin_runtime_pb2 as wire
from cyrene_plugin_runtime._generated import direct_plugin_runtime_pb2_grpc as service


class ProbeError(ValueError):
    """The DirectPluginRuntime TLS health response did not match the contract."""


def _read_file(path: Path, label: str, *, private: bool = False) -> bytes:
    """Read a bounded certificate file without printing its contents."""

    if path.is_symlink() or not path.is_file():
        raise ProbeError(f"{label} is unavailable")
    metadata = path.stat()
    if metadata.st_size == 0 or metadata.st_size > 1024 * 1024:
        raise ProbeError(f"{label} has an invalid size")
    if private and (metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid()):
        raise ProbeError(f"{label} permissions or owner are invalid")
    return path.read_bytes()


def probe(
    target: str, expected_plugin_id: str, root_ca: Path, client_cert: Path, client_key: Path
) -> None:
    """Perform authenticated TLS and require the exact configured plugin identity."""

    if not target or ":" not in target or not expected_plugin_id:
        raise ProbeError("health target and plugin identity are required")
    credentials = grpc.ssl_channel_credentials(
        root_certificates=_read_file(root_ca, "server CA"),
        private_key=_read_file(client_key, "health client key", private=True),
        certificate_chain=_read_file(client_cert, "health client certificate"),
    )
    channel = grpc.secure_channel(target, credentials)
    try:
        grpc.channel_ready_future(channel).result(timeout=3)
        response = service.DirectPluginRuntimeStub(channel).Health(wire.HealthRequest(), timeout=3)
    except grpc.RpcError as error:
        raise ProbeError("DirectPluginRuntime mTLS health request failed") from error
    finally:
        channel.close()
    if (
        response.status != wire.HealthResponse.STATUS_SERVING
        or response.plugin_id != expected_plugin_id
    ):
        raise ProbeError("DirectPluginRuntime did not report its expected serving identity")


def main() -> int:
    """Run the bounded mTLS health check used by Compose."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--plugin-id", required=True)
    parser.add_argument("--server-ca", type=Path, required=True)
    parser.add_argument("--client-cert", type=Path, required=True)
    parser.add_argument("--client-key", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        probe(
            arguments.target,
            arguments.plugin_id,
            arguments.server_ca,
            arguments.client_cert,
            arguments.client_key,
        )
    except (OSError, ProbeError) as error:
        print(f"DirectPluginRuntime health check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
