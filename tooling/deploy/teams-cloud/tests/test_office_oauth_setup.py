"""Exercise the private Microsoft Office OAuth setup boundary and fixed CLI bridge."""

from __future__ import annotations

import http.client
import importlib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest

DEPLOYMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPLOYMENT_ROOT))
setup = importlib.import_module("office_oauth_setup")
release_bundle = importlib.import_module("release_bundle")

TENANT = "11111111-1111-4111-8111-111111111111"
SUBJECT = "22222222-2222-4222-8222-222222222222"
ORGANIZATION = "33333333-3333-4333-8333-333333333333"
WORKSPACE = "44444444-4444-4444-8444-444444444444"
EXTERNAL_SUBJECT = f"{TENANT}:{SUBJECT}"
PROFILE_REF = "profile-office-owner"
LINK_REF = "link-office-owner"
SCOPES = [
    "openid",
    "profile",
    "User.Read",
    "offline_access",
    "Tasks.ReadWrite",
    "Calendars.ReadWrite",
    "Sites.Selected",
]
ENABLED_FEATURES = {
    "plannerEnabled": True,
    "todoEnabled": True,
    "calendarEnabled": True,
    "mailMetadataEnabled": False,
    "mailDraftsEnabled": False,
    "mailSendEnabled": False,
    "sharePointDriveMetadataEnabled": True,
}
EXCHANGE_PROFILE_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,299}$")


def status_document(
    *,
    state: str = "NOT_LINKED",
    auth_status: str = "unknown",
    external_subject: str | None = None,
    revision: int | None = None,
    target_scope: dict[str, Any] | None = None,
    reauth_required: bool = False,
    scopes: list[str] | None = None,
    enabled_features: dict[str, bool] | None = None,
    target_ref_count: int = 0,
) -> dict[str, Any]:
    """Build the exact safe status projection emitted by the fixed Exchange CLI."""

    return {
        "state": state,
        "profileRef": PROFILE_REF,
        "provider": "microsoft_graph",
        "authStatus": auth_status,
        "accessMode": "actor_owned",
        "externalSubject": external_subject,
        "scopeSchemaVersion": 2,
        "targetScope": target_scope,
        "requiredOAuthScopes": list(SCOPES if scopes is None else scopes),
        "credentialRevision": revision,
        "credentialExpiresAt": None,
        "refreshExpiresAt": None,
        "reauthRequired": reauth_required,
        "reauthReasonCode": None,
        "enabledFeatures": dict(ENABLED_FEATURES if enabled_features is None else enabled_features),
        "targetRefCount": target_ref_count,
    }


def operator_context() -> setup.OperatorContext:
    """Build private owner pins that never appear in a browser response."""

    return setup.OperatorContext(
        issuer=f"https://login.microsoftonline.com/{TENANT}/v2.0",
        tenant_id=TENANT,
        subject=SUBJECT,
        organization_id=ORGANIZATION,
        workspace_id=WORKSPACE,
        binding_ref="microsoft-graph.office-owner",
        profile_idempotency_key="office-owner-test-link",
    )


def authorization_url(
    port: int,
    *,
    scopes: str | None = None,
    authority: str = "organizations",
) -> str:
    """Return a test Microsoft authorize URL with the one registered loopback URI."""

    query = urllib.parse.urlencode(
        {
            "client_id": "office-test-client",
            "response_type": "code",
            "response_mode": "query",
            "redirect_uri": f"http://localhost:{port}{setup.CALLBACK_PATH}",
            "scope": scopes
            or "openid profile User.Read offline_access Tasks.ReadWrite Calendars.ReadWrite Sites.Selected",
            "state": "state-secret-123",
            "code_challenge": "A" * 43,
            "code_challenge_method": "S256",
        }
    )
    return f"https://login.microsoftonline.com/{authority}/oauth2/v2.0/authorize?{query}"


class FakeBridge:
    """Provide fixed Exchange CLI responses and retain only test-call evidence."""

    def __init__(self, port: int = setup.DEFAULT_PORT) -> None:
        self.port = port
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.linked = False
        self.status_value = status_document()
        self.discoveries: dict[str, dict[str, Any]] = {
            "planner_plans": {
                "resourceType": "planner_plans",
                "items": [{"id": "plan-1", "displayName": "Operations", "kind": "plan"}],
            },
            "todo_lists": {
                "resourceType": "todo_lists",
                "items": [{"id": "todo-1", "displayName": "My tasks", "kind": "task_list"}],
            },
            "calendars": {
                "resourceType": "calendars",
                "items": [{"id": "calendar-1", "displayName": "Calendar", "kind": "calendar"}],
            },
            "sharepoint_drives": {
                "resourceType": "sharepoint_drives",
                "items": [
                    {
                        "id": "drive-1",
                        "displayName": "Documents",
                        "driveType": "documentLibrary",
                        "siteId": "site-1",
                    }
                ],
            },
            "freebusy_subjects": {
                "resourceType": "freebusy_subjects",
                "items": [
                    {"id": "person@dohorizon.com", "displayName": "Pilot member", "kind": "person"}
                ],
            },
        }

    def invoke(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, dict(payload)))
        if operation == "begin":
            return {
                "state": "AUTHORIZATION_REQUIRED",
                "provider": "microsoft_graph",
                "profileRef": PROFILE_REF,
                "linkSessionRef": LINK_REF,
                "authorizationUrl": authorization_url(self.port),
                "expiresAt": "2026-10-30T10:00:00Z",
            }
        if operation == "status":
            return dict(self.status_value)
        if operation == "complete":
            self.linked = True
            self.status_value = status_document(
                state="LINKED",
                auth_status="healthy",
                external_subject=EXTERNAL_SUBJECT,
                revision=0,
                target_scope={"workspaceId": WORKSPACE},
            )
            return dict(self.status_value)
        if operation == "discover":
            document = dict(self.discoveries[payload["resourceType"]])
            document.update(
                {
                    "state": "DISCOVERED",
                    "pageCursor": payload.get("pageCursor"),
                    "nextCursor": None,
                    "complete": True,
                    "truncated": False,
                    "credentialRevision": self.status_value["credentialRevision"],
                }
            )
            return document
        if operation == "select-targets":
            revision = int(payload["expectedCredentialRevision"]) + 1
            self.status_value = status_document(
                state="UPDATED",
                auth_status="healthy",
                external_subject=EXTERNAL_SUBJECT,
                revision=revision,
                target_scope={"workspaceId": WORKSPACE, **payload["targets"]},
                target_ref_count=sum(len(rows) for rows in payload["targets"].values()),
            )
            return dict(self.status_value)
        raise AssertionError(f"unexpected operation: {operation}")


@pytest.fixture
def linked_service() -> tuple[setup.OfficeSetupService, FakeBridge, setup.SetupSession]:
    """Create an owner profile fixture whose identity was checked by Exchange."""

    bridge = FakeBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()
    bridge.status_value = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=0,
        target_scope={"workspaceId": WORKSPACE},
    )
    return service, bridge, session


def test_operator_template_matches_exact_private_schema_shape() -> None:
    """Keep operator identity pins separate from provider tokens, scopes, and targets."""

    schema = json.loads((DEPLOYMENT_ROOT / "config/office-operator.schema.json").read_text())
    template = json.loads((DEPLOYMENT_ROOT / "config/office-operator.template.json").read_text())
    assert schema["required"] == ["schemaVersion", "actor", "bindingRef", "profileIdempotencyKey"]
    assert schema["additionalProperties"] is False
    assert set(template) == {"schemaVersion", "actor", "bindingRef", "profileIdempotencyKey"}
    assert set(template["actor"]) == {"issuer", "subject", "organizationId", "workspaceId"}
    assert template["bindingRef"] == operator_context().binding_ref
    assert EXCHANGE_PROFILE_ALIAS_RE.fullmatch(template["bindingRef"])
    serialized = json.dumps(template).casefold()
    assert "token" not in serialized
    assert "secret" not in serialized
    assert "mail" not in serialized
    assert "officev2" not in serialized


def test_office_setup_runtime_and_contract_are_in_release_tool_closure() -> None:
    """Package the temporary UI and its exact operator contract with deployment tools."""

    assert {
        "office_oauth_setup.py",
        "config/office-operator.schema.json",
        "config/office-operator.template.json",
    } <= release_bundle.AUTOMATION_DEPLOYMENT_TOOL_FILES


def test_operator_config_pins_exact_owner_and_workspace() -> None:
    """Parse the server-private pins and reject identity or unknown-field drift."""

    template = json.loads((DEPLOYMENT_ROOT / "config/office-operator.template.json").read_text())
    context = setup._operator_context_from_document(template)
    assert context.tenant_id == "00000000-0000-4000-8000-000000000001"
    assert context.external_subject == f"{context.tenant_id}:00000000-0000-4000-8000-000000000002"
    assert context.workspace_id == "00000000-0000-4000-8000-000000000004"
    invalid = dict(template)
    invalid["oauthToken"] = "must-not-exist"
    with pytest.raises(setup.OfficeSetupError, match="office_operator_config_invalid"):
        setup._operator_context_from_document(invalid)


def test_private_operator_config_requires_owner_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read only a regular, single-link, owner-only JSON file without following symlinks."""

    template = json.loads((DEPLOYMENT_ROOT / "config/office-operator.template.json").read_text())
    path = tmp_path / "office-operator.json"
    path.write_text(json.dumps(template), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(setup, "OWNER_UID", os.geteuid())
    context = setup.load_private_operator_config(str(path))
    assert context.workspace_id == "00000000-0000-4000-8000-000000000004"
    path.chmod(0o644)
    with pytest.raises(setup.OfficeSetupError, match="office_operator_config_permissions_invalid"):
        setup.load_private_operator_config(str(path))


def test_cli_bridge_uses_fixed_argv_and_keeps_callback_values_on_stdin() -> None:
    """Never put OAuth code/state in process arguments, stderr, or command text."""

    seen: dict[str, Any] = {}

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen["command"] = command
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b'{"ok":true}\n', stderr=b"")

    bridge = setup.ExchangeCliBridge(runner=runner)
    response = bridge.invoke("complete", {"state": "state-secret", "code": "code-secret"})
    assert response == {"ok": True}
    command = seen["command"]
    assert command == [
        setup.EXCHANGE_CLI,
        "integrations",
        "office-setup",
        "complete",
        "--integration-database",
        setup.INTEGRATION_DATABASE,
        "--private-config",
        setup.PRIVATE_OPERATOR_CONFIG,
    ]
    assert "state-secret" not in command and "code-secret" not in command
    assert json.loads(seen["input"]) == {"state": "state-secret", "code": "code-secret"}
    assert seen["stderr"] == subprocess.DEVNULL


def test_cli_bridge_rejects_multiline_and_non_json_output() -> None:
    """Only one safe JSON result line may cross from the fixed CLI to the page."""

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, stdout=b'{"ok":true}\nsecret\n', stderr=b"")

    with pytest.raises(setup.OfficeSetupError, match="office_bridge_response_invalid"):
        setup.ExchangeCliBridge(runner=runner).invoke("status", {})


def test_authorization_url_accepts_only_loopback_and_no_mail_scope() -> None:
    """Pin Microsoft sign-in to the registered localhost callback and deny Mail scopes."""

    url = authorization_url(setup.DEFAULT_PORT)
    assert (
        setup._validate_authorization_url(
            url,
            port=setup.DEFAULT_PORT,
            tenant_id=TENANT,
            enabled_features=ENABLED_FEATURES,
        )
        == url
    )
    with pytest.raises(setup.OfficeSetupError, match="office_callback_uri_invalid"):
        setup._validate_authorization_url(
            url.replace("localhost", "127.0.0.1"),
            port=setup.DEFAULT_PORT,
            tenant_id=TENANT,
            enabled_features=ENABLED_FEATURES,
        )
    mail_url = authorization_url(
        setup.DEFAULT_PORT,
        scopes="openid https://graph.microsoft.com/Mail.Read",
    )
    with pytest.raises(setup.OfficeSetupError, match="office_mail_scope_forbidden"):
        setup._validate_authorization_url(
            mail_url,
            port=setup.DEFAULT_PORT,
            tenant_id=TENANT,
            enabled_features=ENABLED_FEATURES,
        )
    with pytest.raises(setup.OfficeSetupError, match="office_authorization_url_invalid"):
        setup._validate_authorization_url(
            authorization_url(setup.DEFAULT_PORT, authority=SUBJECT),
            port=setup.DEFAULT_PORT,
            tenant_id=TENANT,
            enabled_features=ENABLED_FEATURES,
        )
    no_refresh = authorization_url(
        setup.DEFAULT_PORT,
        scopes="openid profile User.Read Tasks.ReadWrite Calendars.ReadWrite Sites.Selected",
    )
    with pytest.raises(setup.OfficeSetupError, match="office_oauth_scope_policy_invalid"):
        setup._validate_authorization_url(
            no_refresh,
            port=setup.DEFAULT_PORT,
            tenant_id=TENANT,
            enabled_features=ENABLED_FEATURES,
        )


def test_begin_and_callback_keep_code_and_state_inside_cli_stdin() -> None:
    """Exchange validates the OAuth callback; the setup service does not retain its values."""

    bridge = FakeBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()
    auth_url = service.begin(session)
    assert "state-secret-123" in auth_url
    service.complete_callback(
        "state=state-secret-123&code=code-secret-456&session_state=discard-me&client_info=discard-too"
    )
    operation, payload = bridge.calls[-1]
    assert operation == "complete"
    assert payload == {"state": "state-secret-123", "code": "code-secret-456"}
    assert "code-secret-456" not in repr(service._active_flow)
    safe = service.status(session)
    assert safe["identityVerified"] is True
    assert safe["credentialRevision"] == 0
    assert safe["message"] == "AUTHORIZATION_COMPLETE"
    assert "externalSubject" not in safe
    service.discover(session, "todo_lists")
    targets = {
        "plannerPlanIds": [],
        "sharePointListIds": [],
        "sharePointDriveIds": [],
        "toDoTaskListIds": ["todo-1"],
        "calendarIds": [],
        "freebusySubjects": [],
    }
    updated = service.select_targets(session, expected_revision=0, targets_value=targets)
    assert updated["credentialRevision"] == 1
    final_status = service.status(session)
    assert final_status["identityVerified"] is True
    assert final_status["credentialRevision"] == 1


def test_status_requires_actor_owned_matching_provider_and_never_mail_scope() -> None:
    """Reject unexpected owner access modes and any accidental Mail grant."""

    bad = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=0,
        target_scope={"workspaceId": WORKSPACE},
        scopes=["openid", "Mail.Read"],
    )
    with pytest.raises(setup.OfficeSetupError, match="office_mail_scope_forbidden"):
        setup._safe_status(bad, operator_context())
    bad_owner = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=0,
        target_scope={"workspaceId": WORKSPACE},
    )
    bad_owner["accessMode"] = "workspace_operator"
    assert setup._safe_status(bad_owner, operator_context())["identityVerified"] is False


def test_discovery_and_cas_selection_accept_only_server_observed_targets(
    linked_service: tuple[setup.OfficeSetupService, FakeBridge, setup.SetupSession],
) -> None:
    """Pass only discovered IDs through the fixed owner-bound CAS target update."""

    service, bridge, session = linked_service
    for resource_type in setup.ALLOWED_DISCOVERY_TYPES:
        service.discover(session, resource_type)
    targets = {
        "plannerPlanIds": ["plan-1"],
        "sharePointListIds": [],
        "sharePointDriveIds": ["drive-1"],
        "toDoTaskListIds": ["todo-1"],
        "calendarIds": ["calendar-1"],
        "freebusySubjects": ["person@dohorizon.com"],
    }
    result = service.select_targets(session, expected_revision=0, targets_value=targets)
    assert result["state"] == "UPDATED"
    assert result["credentialRevision"] == 1
    operation, payload = bridge.calls[-1]
    assert operation == "select-targets"
    assert payload == {"expectedCredentialRevision": 0, "targets": targets}
    assert "scopes" not in payload and "tenantId" not in payload and "workspaceId" not in payload
    assert service.status(session)["credentialRevision"] == 1


def test_discovery_follows_opaque_continuation_only_inside_the_server() -> None:
    """Collect complete owner metadata pages and never expose a signed provider cursor."""

    class PagingBridge(FakeBridge):
        def invoke(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            if operation != "discover" or payload.get("resourceType") != "todo_lists":
                return super().invoke(operation, payload)
            self.calls.append((operation, dict(payload)))
            cursor = payload.get("pageCursor")
            if cursor is None:
                return {
                    "state": "DISCOVERED",
                    "resourceType": "todo_lists",
                    "items": [{"id": "todo-1", "displayName": "One", "kind": "task_list"}],
                    "pageCursor": None,
                    "nextCursor": "signed-cursor-private",
                    "complete": False,
                    "truncated": False,
                    "credentialRevision": 0,
                }
            return {
                "state": "DISCOVERED",
                "resourceType": "todo_lists",
                "items": [{"id": "todo-2", "displayName": "Two", "kind": "task_list"}],
                "pageCursor": cursor,
                "nextCursor": None,
                "complete": True,
                "truncated": False,
                "credentialRevision": 0,
            }

    bridge = PagingBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()
    bridge.status_value = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=0,
        target_scope={"workspaceId": WORKSPACE},
    )
    discovered = service.discover(session, "todo_lists")
    assert [item["id"] for item in discovered["items"]] == ["todo-1", "todo-2"]
    assert "signed-cursor-private" not in json.dumps(discovered)
    page_calls = [payload for operation, payload in bridge.calls if operation == "discover"]
    assert page_calls == [
        {"resourceType": "todo_lists"},
        {"resourceType": "todo_lists", "pageCursor": "signed-cursor-private"},
    ]


def test_cas_rejects_ids_not_returned_by_exchange_discovery(
    linked_service: tuple[setup.OfficeSetupService, FakeBridge, setup.SetupSession],
) -> None:
    """The page cannot widen the target set by posting an arbitrary Graph resource ID."""

    service, bridge, session = linked_service
    service.discover(session, "todo_lists")
    targets = {
        "plannerPlanIds": [],
        "sharePointListIds": [],
        "sharePointDriveIds": [],
        "toDoTaskListIds": ["attacker-chosen-id"],
        "calendarIds": [],
        "freebusySubjects": [],
    }
    before = len(bridge.calls)
    with pytest.raises(setup.OfficeSetupError, match="office_target_not_discovered"):
        service.select_targets(session, expected_revision=0, targets_value=targets)
    assert len(bridge.calls) == before + 1
    assert bridge.calls[-1][0] == "status"


def test_freebusy_selection_cannot_clear_a_nonempty_host_allowlist(
    linked_service: tuple[setup.OfficeSetupService, FakeBridge, setup.SetupSession],
) -> None:
    """Keep at least one fixed host-authorized person when Calendar free/busy is enabled."""

    service, bridge, session = linked_service
    service.discover(session, "freebusy_subjects")
    targets = {
        "plannerPlanIds": [],
        "sharePointListIds": [],
        "sharePointDriveIds": [],
        "toDoTaskListIds": [],
        "calendarIds": [],
        "freebusySubjects": [],
    }
    before = len(bridge.calls)
    with pytest.raises(setup.OfficeSetupError, match="office_freebusy_target_required"):
        service.select_targets(session, expected_revision=0, targets_value=targets)
    assert not any(operation == "select-targets" for operation, _ in bridge.calls[before:])


def _start_server(bridge: FakeBridge) -> tuple[setup.OfficeSetupServer, threading.Thread, int]:
    """Start one local HTTP server and bind the fake OAuth URL to its assigned port."""

    service = setup.OfficeSetupService(bridge, operator_context=operator_context(), port=0)
    server = setup.OfficeSetupServer(("127.0.0.1", 0), service, server_nonce="test-nonce")
    port = server.server_address[1]
    service.port = port
    bridge.port = port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def _page_session(port: int) -> tuple[str, str, str]:
    """Create a browser session through the canonical localhost origin."""

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
    response = connection.getresponse()
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.getheader("Location") == f"http://localhost:{port}/"
    response.read()
    connection.request("GET", "/", headers={"Host": f"localhost:{port}"})
    response = connection.getresponse()
    page = response.read().decode("utf-8")
    assert response.status == HTTPStatus.OK
    assert "default-src 'none'" in response.getheader("Content-Security-Policy", "")
    assert "connect-src 'self'" in response.getheader("Content-Security-Policy", "")
    cookie = response.getheader("Set-Cookie").split(";", maxsplit=1)[0]
    csrf = re.search(r'name="csrf" value="([^"]+)"', page)
    assert csrf is not None
    connection.close()
    return cookie, csrf.group(1), page


def test_loopback_accepts_localhost_and_127_but_requires_matching_origin_and_csrf() -> None:
    """Accept matching loopback spellings while rejecting cross-origin and invalid-CSRF writes."""

    bridge = FakeBridge()
    bridge.status_value = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=0,
        target_scope={"workspaceId": WORKSPACE},
    )
    server, thread, port = _start_server(bridge)
    try:
        cookie, csrf, page = _page_session(port)
        assert "Account Center" in page
        assert "Tasks.Read" not in page
        assert "Mail.Read" not in page

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(
            "POST",
            "/api/discover",
            body=json.dumps({"csrf": csrf, "resourceType": "todo_lists"}),
            headers={
                "Host": f"localhost:{port}",
                "Origin": f"http://127.0.0.1:{port}",
                "Cookie": cookie,
                "Content-Type": "application/json",
                "X-Cyrene-CSRF": csrf,
            },
        )
        mismatched = connection.getresponse()
        assert mismatched.status == HTTPStatus.FORBIDDEN
        mismatched.read()
        assert bridge.calls == []

        connection.request(
            "POST",
            "/api/discover",
            body=json.dumps({"csrf": csrf, "resourceType": "todo_lists"}),
            headers={
                "Host": f"localhost:{port}",
                "Origin": f"http://localhost:{port}",
                "Cookie": cookie,
                "Content-Type": "application/json",
                "X-Cyrene-CSRF": "wrong",
            },
        )
        csrf_denied = connection.getresponse()
        assert csrf_denied.status == HTTPStatus.FORBIDDEN
        csrf_denied.read()
        assert bridge.calls == []

        connection.request(
            "POST",
            "/api/discover",
            body=json.dumps({"csrf": csrf, "resourceType": "todo_lists"}),
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": f"http://127.0.0.1:{port}",
                "Cookie": cookie,
                "Content-Type": "application/json",
                "X-Cyrene-CSRF": csrf,
            },
        )
        accepted = connection.getresponse()
        result = json.loads(accepted.read().decode("utf-8"))
        assert accepted.status == HTTPStatus.OK
        assert result["items"][0]["id"] == "todo-1"
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_callback_redirect_clears_code_and_state_and_only_cli_receives_them() -> None:
    """The callback is local, query-free on redirect, and never reflects its secrets."""

    bridge = FakeBridge()
    server, thread, port = _start_server(bridge)
    try:
        cookie, csrf, _ = _page_session(port)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        form = urllib.parse.urlencode({"csrf": csrf})
        connection.request(
            "POST",
            "/authorize",
            body=form,
            headers={
                "Host": f"localhost:{port}",
                "Origin": f"http://localhost:{port}",
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(form)),
            },
        )
        begun = connection.getresponse()
        location = begun.getheader("Location")
        body = begun.read().decode("utf-8")
        assert begun.status == HTTPStatus.SEE_OTHER
        assert location.startswith("https://login.microsoftonline.com/")
        assert "state-secret-123" in location
        assert "state-secret-123" not in body
        assert "code-secret" not in body

        connection.request(
            "GET",
            f"{setup.CALLBACK_PATH}?state=state-secret-123&code=code-secret-456&session_state=discard",
            headers={"Host": f"localhost:{port}"},
        )
        callback = connection.getresponse()
        callback_body = callback.read().decode("utf-8")
        assert callback.status == HTTPStatus.SEE_OTHER
        assert callback.getheader("Location") == "/"
        assert "state-secret-123" not in callback_body
        assert "code-secret-456" not in callback_body
        assert "state-secret-123" not in callback.getheader("Location")
        assert "code-secret-456" not in callback.getheader("Location")
        assert bridge.calls[-1] == (
            "complete",
            {"state": "state-secret-123", "code": "code-secret-456"},
        )
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_callback_provider_error_is_generic_and_never_reaches_exchange() -> None:
    """A declined consent callback does not echo provider errors or submit partial codes."""

    bridge = FakeBridge()
    server, thread, port = _start_server(bridge)
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(
            "GET",
            f"{setup.CALLBACK_PATH}?error=access_denied&error_description=private-detail&state=state-secret",
            headers={"Host": f"localhost:{port}"},
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == HTTPStatus.SEE_OTHER
        assert response.getheader("Location") == "/"
        assert (
            "access_denied" not in body
            and "private-detail" not in body
            and "state-secret" not in body
        )
        assert not any(operation == "complete" for operation, _ in bridge.calls)
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_callback_redirect_registration_error_surfaces_only_aadsts_code() -> None:
    """Report AADSTS50011 explicitly without echoing its query or using a public fallback."""

    bridge = FakeBridge()
    server, thread, port = _start_server(bridge)
    try:
        cookie, _csrf, _page = _page_session(port)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(
            "GET",
            f"{setup.CALLBACK_PATH}?error=invalid_request&error_description=AADSTS50011%3A+redirect+URI+must+be+registered&state=state-secret",
            headers={"Host": f"localhost:{port}"},
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == HTTPStatus.SEE_OTHER
        assert response.getheader("Location") == "/"
        assert "AADSTS50011" not in body
        assert "state-secret" not in body
        assert not any(operation == "complete" for operation, _ in bridge.calls)

        connection.request(
            "GET",
            "/api/status",
            headers={"Host": f"localhost:{port}", "Cookie": cookie},
        )
        status = connection.getresponse()
        data = json.loads(status.read().decode("utf-8"))
        assert status.status == HTTPStatus.OK
        assert data["message"] == "AADSTS50011"
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
