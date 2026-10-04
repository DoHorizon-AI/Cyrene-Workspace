"""Offline contract tests for the local Native Sidecar invocation helper."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tooling/acceptance/native-components-v2/sidecar_invocation.py"
)
SPEC = importlib.util.spec_from_file_location("sidecar_invocation", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
invocation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = invocation
SPEC.loader.exec_module(invocation)


class FakeProductApi:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


def test_build_invocation_maps_only_product_v2_fields() -> None:
    result = invocation.build_invocation(
        SimpleNamespace(ProductApiInvocationV2=FakeProductApi),
        owner_id="yield",
        operation_id="workspaceStartRun",
        json_body=b'{"draftId":"draft-1"}',
        resource_id="draft-1",
        idempotency_key="one-step-start-1",
    )

    assert result.__dict__ == {
        "owner_id": "yield",
        "operation_id": "workspaceStartRun",
        "json_body": b'{"draftId":"draft-1"}',
        "resource_id": "draft-1",
        "idempotency_key": "one-step-start-1",
    }
    assert not ({"url", "role", "directory_version"} & result.__dict__.keys())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"owner_id": "", "operation_id": "op"}, "owner_id"),
        ({"owner_id": "owner", "operation_id": "../route"}, "operation_id"),
        (
            {"owner_id": "owner", "operation_id": "op", "idempotency_key": "x" * 201},
            "idempotency_key",
        ),
    ],
)
def test_build_invocation_rejects_invalid_identifiers(kwargs: dict[str, str], message: str) -> None:
    common: dict[str, object] = {
        "product_api_pb2": SimpleNamespace(ProductApiInvocationV2=FakeProductApi),
        "owner_id": "owner",
        "operation_id": "operation",
        "json_body": b"{}",
    }
    common.update(kwargs)
    with pytest.raises(invocation.InvocationError, match=message):
        invocation.build_invocation(**common)  # type: ignore[arg-type]


def test_private_token_requires_absolute_private_regular_file(tmp_path: Path) -> None:
    token_file = tmp_path / "caller.token"
    token_file.write_text("fixture-caller-token-123456\n", encoding="ascii")
    os.chmod(token_file, 0o600)

    assert invocation.read_private_token(token_file, local=False) == "fixture-caller-token-123456"
    os.chmod(token_file, 0o640)
    with pytest.raises(invocation.InvocationError, match="group or other access"):
        invocation.read_private_token(token_file, local=False)


def test_private_token_rejects_symlink(tmp_path: Path) -> None:
    token_file = tmp_path / "caller.token"
    target = tmp_path / "target.token"
    target.write_text("fixture-caller-token-123456", encoding="ascii")
    os.chmod(target, 0o600)
    token_file.symlink_to(target)

    with pytest.raises(invocation.InvocationError, match="regular file"):
        invocation.read_private_token(token_file, local=False)


def test_local_token_accepts_valid_private_file(tmp_path: Path) -> None:
    token_file = tmp_path / "sidecar.local-token"
    token_file.write_bytes(b"A" * 29 + b"-._\n")
    os.chmod(token_file, 0o600)

    assert invocation.read_private_token(token_file, local=True) == "A" * 29 + "-._"


@pytest.mark.parametrize("token", [b"A" * 31, b"!" * 32])
def test_local_token_rejects_invalid_length_or_characters(tmp_path: Path, token: bytes) -> None:
    token_file = tmp_path / "sidecar.local-token"
    token_file.write_bytes(token)
    os.chmod(token_file, 0o600)

    with pytest.raises(invocation.InvocationError, match="local Sidecar token has invalid format"):
        invocation.read_private_token(token_file, local=True)


def test_execute_negotiates_v2_and_sends_through_sidecar_stub() -> None:
    call_log: list[tuple[str, object, object]] = []
    response_body = json.dumps({"runId": "run-1"}).encode()

    class FakeChannel:
        def close(self) -> None:
            call_log.append(("close", None, None))

    class FakeGrpc:
        @staticmethod
        def insecure_channel(target: str) -> FakeChannel:
            call_log.append(("channel", target, None))
            return FakeChannel()

    class FakeStub:
        def __init__(self, channel: FakeChannel) -> None:
            self.channel = channel

        def NegotiateVersion(self, request: object, **kwargs: object) -> object:
            call_log.append(("negotiate", request, kwargs))
            return SimpleNamespace(selected_version=2, supported_versions=[2])

        def Execute(self, request: object, **kwargs: object) -> object:
            call_log.append(("execute", request, kwargs))
            return SimpleNamespace(
                state=4,
                error_code="",
                product_response=SimpleNamespace(
                    status_code=201,
                    content_type="application/json",
                    json_body=response_body,
                ),
                HasField=lambda field: field == "product_response",
            )

    local_pb2 = SimpleNamespace(
        NegotiateVersionRequest=lambda **kwargs: SimpleNamespace(**kwargs),
        ExecuteRequest=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    local_grpc = SimpleNamespace(WorkspaceSidecarServiceStub=FakeStub)
    product_call = FakeProductApi(owner_id="yield", operation_id="workspaceGetRun")
    result = invocation.execute_via_sidecar(
        grpc=FakeGrpc,
        local_pb2=local_pb2,
        local_grpc=local_grpc,
        target="unix:///run/cyrene/workspace-sidecar.sock",
        local_token="l" * 40,
        caller_token="fixture-caller-token-123456",
        workspace_id="workspace-1",
        invocation=product_call,
    )

    assert result["state"] == "INVOCATION_STATE_SUCCEEDED"
    assert result["product_response"] == {
        "status_code": 201,
        "content_type": "application/json",
        "json_body": {"runId": "run-1"},
    }
    assert [call[0] for call in call_log] == ["channel", "negotiate", "execute", "close"]
    request = call_log[2][1]
    assert request.workspace_id == "workspace-1"
    assert request.caller_token == "fixture-caller-token-123456"
    assert request.invocation is product_call
    assert call_log[2][2]["metadata"] == (("authorization", f"Bearer {'l' * 40}"),)


def test_execute_rejects_non_uds_target_before_connecting() -> None:
    with pytest.raises(invocation.InvocationError, match="local Sidecar UDS"):
        invocation.execute_via_sidecar(
            grpc=SimpleNamespace(),
            local_pb2=SimpleNamespace(),
            local_grpc=SimpleNamespace(),
            target="https://not-a-relay-or-sidecar",
            local_token="unused",
            caller_token="unused",
            workspace_id="workspace-1",
            invocation=object(),
        )


def test_generated_client_executes_over_local_uds_mockserver() -> None:
    pytest.importorskip("grpc")
    pytest.importorskip("grpc_tools")
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    proto_root = os.environ.get("CYRENE_PLUGINS_PROTO_ROOT")
    if not proto_root:
        pytest.skip("set CYRENE_PLUGINS_PROTO_ROOT to the pinned Plugins contracts/proto tree")
    generated_grpc, product_pb2, (local_pb2, local_grpc), generated_temp = (
        invocation._load_generated_client(Path(proto_root))
    )
    observed: list[tuple[str, object, tuple[object, ...]]] = []
    with tempfile.TemporaryDirectory(prefix="cyrene-sidecar-uds-") as temp_dir:
        socket_path = Path(temp_dir) / "sidecar.sock"
        endpoint = f"unix://{socket_path}"

        class MockSidecar(local_grpc.WorkspaceSidecarServiceServicer):
            def NegotiateVersion(self, request: object, context: object) -> object:
                observed.append(("negotiate", request, tuple(context.invocation_metadata())))
                return local_pb2.NegotiateVersionResponse(
                    selected_version=2,
                    supported_versions=[2],
                )

            def Execute(self, request: object, context: object) -> object:
                observed.append(("execute", request, tuple(context.invocation_metadata())))
                return local_pb2.ExecuteResponse(
                    state=4,
                    product_response=product_pb2.ProductApiResponseV2(
                        status_code=201,
                        content_type="application/json",
                        json_body=b'{"draftId":"fixture-draft"}',
                    ),
                )

        executor = ThreadPoolExecutor(max_workers=2)
        server = generated_grpc.server(executor)
        local_grpc.add_WorkspaceSidecarServiceServicer_to_server(MockSidecar(), server)
        assert server.add_insecure_port(endpoint) != 0
        server.start()
        try:
            product_invocation = invocation.build_invocation(
                product_pb2,
                owner_id="yield",
                operation_id="workspaceCreateTrainingDraft",
                json_body=b'{"name":"fixture"}',
                resource_id="",
                idempotency_key="fixture-key",
            )
            result = invocation.execute_via_sidecar(
                grpc=generated_grpc,
                local_pb2=local_pb2,
                local_grpc=local_grpc,
                target=endpoint,
                local_token="l" * 40,
                caller_token="fixture-caller-token-123456",
                workspace_id="fixture-workspace",
                invocation=product_invocation,
            )
        finally:
            server.stop(0).wait()
            executor.shutdown(wait=True)
            generated_temp.cleanup()

    assert [entry[0] for entry in observed] == ["negotiate", "execute"]
    assert result["state"] == "INVOCATION_STATE_SUCCEEDED"
    assert result["product_response"]["json_body"] == {"draftId": "fixture-draft"}
    request = observed[1][1]
    assert request.workspace_id == "fixture-workspace"
    assert request.caller_token == "fixture-caller-token-123456"
    assert request.invocation == product_invocation
    assert request.invocation.owner_id == "yield"
    assert request.invocation.operation_id == "workspaceCreateTrainingDraft"
    assert request.invocation.json_body == b'{"name":"fixture"}'
    assert request.invocation.resource_id == ""
    assert request.invocation.idempotency_key == "fixture-key"
    for _, _, metadata in observed:
        assert ("authorization", "Bearer " + "l" * 40) in metadata
