"""Invoke one approved Product v2 operation through the local Workspace Sidecar.

This operator tool speaks the pinned Plugins local/v2 protobuf over the
Sidecar's private Unix socket. The Sidecar verifies the caller through
Authority and queues the approved invocation; this module has no Product HTTP
client and accepts no route, role, principal, or catalog version.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROTOCOL_LOCK = REPOSITORY_ROOT / "governance/workspace-connection-protocols-v2.lock.json"
PROTO_FILES = {
    "cyrene/workspace/local/v2/workspace_sidecar.proto": "cyrene.workspace.local.v2",
    "cyrene/workspace/authority/v2/workspace_authority.proto": "cyrene.workspace.authority.v2",
    "cyrene/workspace/product/v2/product_api.proto": "cyrene.workspace.product.v2",
}
MAX_BODY_BYTES = 1024 * 1024
MAX_TOKEN_BYTES = 4096
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class InvocationError(ValueError):
    """A safe-to-display input or protocol setup error."""


def build_invocation(
    product_api_pb2: Any,
    *,
    owner_id: str,
    operation_id: str,
    json_body: bytes,
    resource_id: str = "",
    idempotency_key: str = "",
) -> Any:
    """Build the five-field Product v2 invocation without route metadata."""
    _validate_id(owner_id, "owner_id")
    _validate_id(operation_id, "operation_id")
    if resource_id:
        _validate_id(resource_id, "resource_id")
    if idempotency_key and not (1 <= len(idempotency_key) <= 200):
        raise InvocationError("idempotency_key must contain 1..200 characters")
    if len(json_body) > MAX_BODY_BYTES:
        raise InvocationError("JSON body exceeds the 1 MiB limit")
    try:
        body = json.loads(json_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvocationError("JSON body must be valid UTF-8 JSON") from error
    if not isinstance(body, dict):
        raise InvocationError("JSON body must be a JSON object")
    return product_api_pb2.ProductApiInvocationV2(
        owner_id=owner_id,
        operation_id=operation_id,
        json_body=json_body,
        resource_id=resource_id,
        idempotency_key=idempotency_key,
    )


def _validate_id(value: str, label: str) -> None:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise InvocationError(f"{label} must be 1..200 safe identifier characters")


def read_private_token(path: Path, *, local: bool) -> str:
    """Read a bearer only from an absolute, private, non-symlink regular file."""
    if not path.is_absolute():
        raise InvocationError("token file path must be absolute")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InvocationError("token file is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise InvocationError("token path must be a regular file, not a symlink")
    if metadata.st_mode & 0o077:
        raise InvocationError("token file must not grant group or other access")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode) or opened_metadata.st_mode & 0o077:
            os.close(descriptor)
            raise InvocationError("token file changed while being checked")
        with os.fdopen(descriptor, "rb") as token_file:
            value = token_file.read(MAX_TOKEN_BYTES + 1).rstrip(b"\r\n")
    except InvocationError:
        raise
    except OSError as error:
        raise InvocationError("token file could not be read safely") from error
    if not value or len(value) > MAX_TOKEN_BYTES:
        raise InvocationError("token file has invalid length")
    if local:
        if not 32 <= len(value) <= 256 or not all(
            48 <= byte <= 57 or 65 <= byte <= 90 or 97 <= byte <= 122 or byte in b"-_."
            for byte in value
        ):
            raise InvocationError("local Sidecar token has invalid format")
    elif len(value) < 16 or any(byte in b" \t\r\n" for byte in value):
        raise InvocationError("caller token has invalid format")
    try:
        return value.decode("ascii")
    except UnicodeDecodeError as error:
        raise InvocationError("token file must contain ASCII") from error


def _pinned_proto_paths(proto_root: Path) -> list[Path]:
    """Check the Plugins proto mirror against the Workspace protocol lock."""
    try:
        lock = json.loads(PROTOCOL_LOCK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InvocationError("Workspace protocol lock is unavailable or invalid") from error
    if lock.get("lockId") != "workspace-connection-protocols-v2":
        raise InvocationError("unexpected Workspace protocol lock")
    entries = {
        entry.get("apiVersion"): entry
        for entry in lock.get("protocols", [])
        if isinstance(entry, dict)
    }
    resolved: list[Path] = []
    for relative, api_version in PROTO_FILES.items():
        entry = entries.get(api_version)
        mirror = entry.get("mirror") if isinstance(entry, dict) else None
        definition = entry.get("definition") if isinstance(entry, dict) else None
        if not isinstance(mirror, dict) or not isinstance(definition, dict):
            raise InvocationError(f"{api_version} is absent from the protocol lock")
        expected = mirror.get("sha256")
        if (
            mirror.get("repository") != "DoHorizon-AI/Cyrene-Plugins-Official"
            or mirror.get("path") != f"contracts/proto/{relative}"
            or expected != definition.get("sha256")
        ):
            raise InvocationError(f"{api_version} has an unexpected protocol lock entry")
        proto = proto_root / relative
        try:
            actual = hashlib.sha256(proto.read_bytes()).hexdigest()
        except OSError as error:
            raise InvocationError(f"pinned proto file is unavailable: {relative}") from error
        if actual != expected:
            raise InvocationError(f"pinned proto hash mismatch: {relative}")
        resolved.append(proto)
    return resolved


def _load_generated_client(
    proto_root: Path,
) -> tuple[Any, Any, Any, tempfile.TemporaryDirectory[str]]:
    """Generate the official Python gRPC client from the pinned proto source."""
    try:
        import grpc  # type: ignore[import-not-found]
        import grpc_tools
        from grpc_tools import protoc
    except ImportError as error:
        raise InvocationError(
            "Python gRPC client generation needs staged grpcio and grpcio-tools packages"
        ) from error
    proto_files = _pinned_proto_paths(proto_root)
    temp = tempfile.TemporaryDirectory(prefix="cyrene-sidecar-proto-")
    os.chmod(temp.name, 0o700)
    output = Path(temp.name)
    grpc_include = Path(grpc_tools.__file__).resolve().parent / "_proto"
    arguments = [
        "grpc_tools.protoc",
        f"-I{proto_root}",
        f"-I{grpc_include}",
        f"--python_out={output}",
        f"--grpc_python_out={output}",
        *(str(path) for path in proto_files),
    ]
    if protoc.main(arguments) != 0:
        temp.cleanup()
        raise InvocationError("pinned local/v2 protobuf client generation failed")
    sys.path.insert(0, str(output))
    try:
        product_pb2 = importlib.import_module("cyrene.workspace.product.v2.product_api_pb2")
        local_pb2 = importlib.import_module("cyrene.workspace.local.v2.workspace_sidecar_pb2")
        local_grpc = importlib.import_module("cyrene.workspace.local.v2.workspace_sidecar_pb2_grpc")
    except ImportError as error:
        temp.cleanup()
        raise InvocationError(
            "generated local/v2 Python gRPC client could not be imported"
        ) from error
    return grpc, product_pb2, (local_pb2, local_grpc), temp


def execute_via_sidecar(
    *,
    grpc: Any,
    local_pb2: Any,
    local_grpc: Any,
    target: str,
    local_token: str,
    caller_token: str,
    workspace_id: str,
    invocation: Any,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Negotiate local/v2 then make one Authority-approved Sidecar Execute call."""
    _validate_id(workspace_id, "workspace_id")
    if not target.startswith("unix:///"):
        raise InvocationError("only the configured local Sidecar UDS target is accepted")
    metadata = (("authorization", f"Bearer {local_token}"),)
    channel = grpc.insecure_channel(target)
    try:
        stub = local_grpc.WorkspaceSidecarServiceStub(channel)
        version = stub.NegotiateVersion(
            local_pb2.NegotiateVersionRequest(minimum_version=2, maximum_version=2),
            metadata=metadata,
            timeout=timeout_seconds,
        )
        if version.selected_version != 2 or 2 not in version.supported_versions:
            raise InvocationError("Sidecar did not negotiate local/v2")
        response = stub.Execute(
            local_pb2.ExecuteRequest(
                workspace_id=workspace_id,
                caller_token=caller_token,
                invocation=invocation,
            ),
            metadata=metadata,
            timeout=timeout_seconds,
        )
        result: dict[str, Any] = {
            "state": _enum_name(local_pb2, response.state),
            "error_code": response.error_code or None,
        }
        if response.HasField("product_response"):
            product_response = response.product_response
            result["product_response"] = {
                "status_code": product_response.status_code,
                "content_type": product_response.content_type,
                "json_body": _decode_product_body(product_response.json_body),
            }
        return result
    finally:
        channel.close()


def _enum_name(local_pb2: Any, number: int) -> str:
    try:
        authority = next(
            dependency
            for dependency in local_pb2.DESCRIPTOR.dependencies
            if dependency.name.endswith("workspace_authority.proto")
        )
        return authority.enum_types_by_name["InvocationState"].values_by_number[number].name
    except (AttributeError, KeyError, IndexError, StopIteration):
        pass
    # The exact enum names are pinned by workspace_authority.proto.
    return {
        0: "INVOCATION_STATE_UNSPECIFIED",
        1: "INVOCATION_STATE_PENDING",
        2: "INVOCATION_STATE_CLAIMED",
        3: "INVOCATION_STATE_ACKNOWLEDGED",
        4: "INVOCATION_STATE_SUCCEEDED",
        5: "INVOCATION_STATE_FAILED",
        6: "INVOCATION_STATE_UNKNOWN_RESULT",
    }.get(number, f"UNKNOWN_{number}")


def _decode_product_body(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"encoding": "base64", "value": base64.b64encode(raw).decode("ascii")}


def _read_body(path: Path) -> bytes:
    if not path.is_absolute():
        raise InvocationError("JSON body file path must be absolute")
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise InvocationError("JSON body path must be a regular non-symlink file")
        if metadata.st_size > MAX_BODY_BYTES:
            raise InvocationError("JSON body exceeds the 1 MiB limit")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise InvocationError("JSON body path changed while being checked")
        with os.fdopen(descriptor, "rb") as body_file:
            body = body_file.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            raise InvocationError("JSON body exceeds the 1 MiB limit")
        json_value = json.loads(body)
    except (OSError, json.JSONDecodeError) as error:
        raise InvocationError("JSON body file is unreadable or invalid JSON") from error
    if not isinstance(json_value, dict):
        raise InvocationError("JSON body must be a JSON object")
    return body


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uds", required=True, help="Absolute local Sidecar Unix socket path")
    parser.add_argument("--proto-root", required=True, help="Plugins contracts/proto root")
    parser.add_argument("--caller-token-file", required=True, help="Private caller bearer file")
    parser.add_argument(
        "--local-token-file",
        default="/etc/cyrene/workspace-sidecar.local-token",
        help="Private local Sidecar bearer file (default matches Sidecar configuration)",
    )
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--json-body-file", help="JSON object file; defaults to an empty object")
    parser.add_argument("--resource-id", default="")
    parser.add_argument("--idempotency-key", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        uds = Path(args.uds)
        proto_root = Path(args.proto_root)
        caller_token = read_private_token(Path(args.caller_token_file), local=False)
        local_token = read_private_token(Path(args.local_token_file), local=True)
        body = _read_body(Path(args.json_body_file)) if args.json_body_file else b"{}"
        grpc, product_pb2, (local_pb2, local_grpc), temp = _load_generated_client(proto_root)
        try:
            invocation = build_invocation(
                product_pb2,
                owner_id=args.owner_id,
                operation_id=args.operation_id,
                json_body=body,
                resource_id=args.resource_id,
                idempotency_key=args.idempotency_key,
            )
            result = execute_via_sidecar(
                grpc=grpc,
                local_pb2=local_pb2,
                local_grpc=local_grpc,
                target=f"unix://{uds}",
                local_token=local_token,
                caller_token=caller_token,
                workspace_id=args.workspace_id,
                invocation=invocation,
            )
        finally:
            temp.cleanup()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["state"] == "INVOCATION_STATE_SUCCEEDED" else 2
    except InvocationError as error:
        print(f"sidecar invocation refused: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - never expose gRPC metadata or bearer values.
        print(f"sidecar invocation failed: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
