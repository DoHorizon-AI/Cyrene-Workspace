"""Static contract checks for the source-free modular clean-host workflow."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import subprocess
import textwrap
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/modular-distribution-acceptance.yml"
PINS_SCHEMA = ROOT / "tooling/acceptance/modular-distribution-v01/release-pins-v1.schema.json"


def _workflow_driver(tmp_path: Path) -> tuple[dict[str, object], str]:
    """Parse workflow YAML and extract the runner-local driver without executing it."""
    source = WORKFLOW.read_text(encoding="utf-8")
    document = yaml.safe_load(source)
    job = document["jobs"]["clean-host-acceptance"]
    init_step = job["steps"][0]
    match = re.search(
        r'cat > "\$ACCEPTANCE_ROOT/acceptance_driver\.py" <<\'PY\'\n(.*?)\nPY\npython3',
        init_step["run"],
        re.DOTALL,
    )
    assert match is not None
    driver = match.group(1)
    compile(driver, "acceptance_driver.py", "exec")
    parsed_driver = ast.parse(driver)
    for node in ast.walk(parsed_driver):
        if not isinstance(node, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == "code" for target in node.targets
        ):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "dedent"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
        ):
            compile(textwrap.dedent(value.args[0].value), "acceptance-helper.py", "exec")
    driver_path = tmp_path / "acceptance_driver.py"
    driver_path.write_text(driver, encoding="utf-8")
    return document, str(driver_path)


def _valid_pins() -> dict[str, object]:
    """Build a complete synthetic identity set; digests are shape fixtures only."""
    native_sha = "1" * 40
    catalog_source = "2" * 40
    product_source = "3" * 40
    catalog_sha = "b" * 64
    repository = "DoHorizon-AI/Cyrene-Catalyst"
    component_release = f"preview-{product_source}"

    def component(component_id: str, target_id: str) -> dict[str, object]:
        """Create one synthetic pinned release row for local contract tests."""
        return {
            "componentId": component_id,
            "targetId": target_id,
            "version": "1.0.0",
            "releaseId": component_release,
            "manifestDigest": "sha256:" + "c" * 64,
            "manifestAssetDigest": "sha256:" + "d" * 64,
            "digest": "sha256:" + "e" * 64,
            "publisherIdentity": {
                "repository": repository,
                "workflow": repository + "/.github/workflows/component-release.yml",
            },
            "indexIdentity": {
                "repository": repository,
                "assetName": "component-release-index-v1.json",
                "assetDigest": "sha256:" + "f" * 64,
                "indexDigest": "sha256:" + "a" * 64,
                "releaseTag": component_release,
            },
            "attestationRef": {
                "repository": repository,
                "workflow": repository + "/.github/workflows/component-release.yml",
                "sourceCommit": product_source,
                "subjectDigest": "sha256:" + "d" * 64,
            },
        }

    selected_components = [
        component("cyrene-catalyst", "linux-ubuntu-24.04-x86_64-python-3.12"),
        component("cyrene-client-workspace-control", "linux-ubuntu-24.04-x86_64-node-24"),
        component("cyrene-client-workspace-web", "linux-ubuntu-24.04-x86_64-web"),
        component(
            "cyrene-runtime-maintenance-sdk", "linux-ubuntu-24.04-x86_64-python-3.12-library"
        ),
        component("cyrene-tools-dataset-generation", "linux-ubuntu-24.04-x86_64-python-3.12"),
        component("cyrene-tools-dataset-preparation", "linux-ubuntu-24.04-x86_64-python-3.12"),
        component("cyrene-tools-document-parsing", "linux-ubuntu-24.04-x86_64-python-3.12"),
        component("cyrene-tools-knowledge-preparation", "linux-ubuntu-24.04-x86_64-python-3.12"),
    ]
    return {
        "schemaVersion": 1,
        "nativeInstaller": {
            "releaseId": f"native-installer-preview-{native_sha}",
            "sourceRepository": "DoHorizon-AI/Cyrene-Workspace",
            "sourceRef": "refs/heads/develop",
            "sourceCommit": native_sha,
            "workflow": "DoHorizon-AI/Cyrene-Workspace/.github/workflows/native-installer-release.yml",
            "targetId": "linux-ubuntu-24.04-x86_64-python-3.12",
            "debAssetName": "cyrene_1.0.0_ubuntu-24.04_amd64.deb",
            "debSha256": "4" * 64,
            "debSizeBytes": 4096,
        },
        "catalog": {
            "releaseId": f"catalog-v2-preview-{catalog_source}",
            "repository": "DoHorizon-AI/Cyrene-Workspace",
            "workflow": "DoHorizon-AI/Cyrene-Workspace/.github/workflows/component-catalog-release.yml",
            "sourceRef": "refs/heads/develop",
            "sourceCommit": catalog_source,
            "assetName": "component-catalog-v2.json",
            "sha256": catalog_sha,
            "sizeBytes": 8192,
            "attestationAssetName": "component-catalog-v2.json.attestation.jsonl",
            "attestationSha256": "5" * 64,
            "schemaVersion": 2,
            "generation": 15,
        },
        "workloads": {
            "catalyst": {
                "workloadId": "catalyst",
                "targetId": "linux-ubuntu-24.04-x86_64",
                "catalogDigest": "sha256:" + catalog_sha,
                "service": {
                    "unit": "cyrene-catalyst.service",
                    "baseUrl": "http://127.0.0.1:8004",
                    "healthPath": "/healthz",
                },
                "selectedComponents": selected_components,
            }
        },
    }


def _load_driver_module(tmp_path: Path, name: str) -> object:
    """Load the workflow-local driver after compiling its embedded Python source."""
    _, driver_path = _workflow_driver(tmp_path)
    spec = importlib.util.spec_from_file_location(name, driver_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verified_catalyst_catalog() -> dict[str, object]:
    """Build the minimal signed-catalog shape consumed by binding readback tests."""
    plugin_components = {
        "cyrene-tools-dataset-generation": "cyrene.tools.dataset-generation",
        "cyrene-tools-dataset-preparation": "cyrene.tools.dataset-preparation",
        "cyrene-tools-document-parsing": "cyrene.tools.document-parsing",
        "cyrene-tools-knowledge-preparation": "cyrene.tools.knowledge-preparation",
    }
    return {
        "schemaVersion": 2,
        "generation": 15,
        "components": [
            {"componentId": component_id, "pluginPackage": {"packageId": package_id}}
            for component_id, package_id in plugin_components.items()
        ],
        "workloads": [
            {
                "workloadId": "catalyst",
                "sourcePolicy": {
                    "mode": "actualProduct",
                    "productSources": [
                        {"componentId": "cyrene-catalyst", "sourceId": "cyrene-catalyst"}
                    ],
                    "operations": [
                        "activate",
                        "recover_binding",
                        "deactivate",
                        "runtime_status",
                        "get_installation",
                    ],
                },
                "bindings": [
                    {
                        "componentId": component_id,
                        "bindingId": f"catalog-binding-actual-product-cyrene-catalyst-{component_id}",
                    }
                    for component_id in plugin_components
                ],
            }
        ],
    }


def test_workflow_has_no_source_checkout_and_compiles_embedded_driver(tmp_path: Path) -> None:
    """The fresh host consumes official release assets, not repository source."""
    document, _ = _workflow_driver(tmp_path)
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "actions/checkout@" not in source
    assert "git clone" not in source
    assert document["jobs"]["clean-host-acceptance"]["runs-on"] == "ubuntu-24.04"
    assert document["jobs"]["clean-host-acceptance"]["permissions"] == {
        "contents": "read",
        "attestations": "read",
    }
    assert '[[ "$GITHUB_REF" == "refs/heads/develop" ]]' in source
    assert '"workflowSha": os.environ.get("GITHUB_WORKFLOW_SHA")' in source
    assert '"native-installer-release-v2.json"' in source
    assert '"native-installer-source-receipt-v2.json"' in source
    assert '"native_static_binding_readback"' in source
    assert '"catalyst_active_receipt_readback"' in source
    assert '"catalyst_runtime_source_activation"' in source
    assert "retain_output=False" in source
    assert "raw Product output was withheld" in source
    assert '"/var/lib/cyrene/runtime/activity-sources.json"' in source
    assert '"/usr/lib/cyrene/scripts/native_package_runtime_bootstrap.py"' in source
    assert '"/etc/cyrene/runtime-activity-source-tokens"' in source
    assert '"tokenHashMatchesCatalog":True' in source
    assert '"bindingScopeCount":len(verified_bindings)' in source
    assert 'phase("client_static_web_http", client_web_http)' in source
    assert '"http://127.0.0.1:8100/"' in source
    assert '"http://127.0.0.1:5182/health/ready"' in source
    assert '"/api/v1/auth/session"' in source
    assert 'status.get("hostMetadata", {}).get("web")' in source
    assert '"csrfTokenStored": False' in source
    assert '"sessionCommandPosted": False' in source
    assert '"/api/v1/catalyst/datasets"' in source
    assert '"/api/v1/datasets"' in source
    assert '"client_release_integration",' in source
    assert '"/etc/cyrene/studio-control.env"' in source
    assert '"STUDIO_CATALYST_API_TOKEN_FILE"' in source
    assert "STUDIO_PUBLIC_ORIGINS=" in source
    assert "native active-pointer proof" in source
    assert 'receipt.get("bundleIdentity") == manifest.get("artifact_digest")' in source
    assert 'Path(str(receipt.get("releasePath"))).resolve(strict=True)' in source
    assert '"client_static_web_http"' in source
    assert 'Path("/var/lib/cyrene-updates")' in source
    assert '".workload-sdk-install.json"' in source
    assert '"staticWeb"' in source
    acceptance_readme = (ROOT / "tooling/acceptance/modular-distribution-v01/README.md").read_text(
        encoding="utf-8"
    )
    assert "required HTTP gate" in acceptance_readme
    assert "all four Catalog-owned plugin bindings" in acceptance_readme
    assert '"/usr/share/cyrene/bootstrap-catalog-binding-v1.json"' in source
    assert '"/usr/share/cyrene/component-catalog-v1.json"' in source
    assert '"/usr/share/cyrene/component-catalog-v2.json"' in source
    assert '"/usr/share/cyrene/native-install-contract-v1.json"' in source
    assert '"workspaceCatalogs"' in source
    assert '"activeV2"' in source
    assert '"installedReceipt"' not in source
    schema = json.loads(PINS_SCHEMA.read_text(encoding="utf-8"))
    assert "installedReceipt" not in schema["properties"]["nativeInstaller"]["properties"]
    assert "native_install_receipt_readback" not in source
    assert (
        '"gh", "api", f"repos/{catalog_pin[\'repository\']}/releases/tags/{catalog_pin[\'releaseId\']}"'
        in source
    )
    assert '"--cert-oidc-issuer", "https://token.actions.githubusercontent.com"' in source
    assert "Runnable Catalyst core" in source


def test_release_pins_schema_and_embedded_preflight_reject_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins must match the schema and fail closed before any native mutation."""
    schema = json.loads(PINS_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    pins = _valid_pins()
    jsonschema.Draft202012Validator(schema).validate(pins)
    _, driver_path = _workflow_driver(tmp_path)
    monkeypatch.setenv("NATIVE_RELEASE_ID", pins["nativeInstaller"]["releaseId"])
    spec = importlib.util.spec_from_file_location("acceptance_driver", driver_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.validate_pin_shape(pins)

    pins["workloads"]["echo"] = {
        "workloadId": "echo",
        "targetId": "linux-ubuntu-24.04-x86_64",
        "catalogDigest": "sha256:" + "b" * 64,
        "selectedComponents": pins["workloads"]["catalyst"]["selectedComponents"],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(pins)
    with pytest.raises(RuntimeError, match="only Catalyst workload pins"):
        module.validate_pin_shape(pins)

    pins.pop("workloads")
    pins["workloads"] = {"catalyst": _valid_pins()["workloads"]["catalyst"]}
    pins["catalog"]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="catalog digest pin differs"):
        module.validate_pin_shape(pins)

    unsafe_pins = _valid_pins()
    unsafe_pins["workloads"]["catalyst"]["selectedComponents"][0]["version"] = "../outside"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(unsafe_pins)
    with pytest.raises(RuntimeError, match="safe immutable path segment"):
        module.validate_pin_shape(unsafe_pins)

    missing_control = _valid_pins()
    missing_control["workloads"]["catalyst"]["selectedComponents"] = [
        row
        for row in missing_control["workloads"]["catalyst"]["selectedComponents"]
        if row["componentId"] != "cyrene-client-workspace-control"
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(missing_control)
    with pytest.raises(RuntimeError, match="Node 24 Client Control"):
        module.validate_pin_shape(missing_control)

    wrong_control_target = _valid_pins()
    next(
        row
        for row in wrong_control_target["workloads"]["catalyst"]["selectedComponents"]
        if row["componentId"] == "cyrene-client-workspace-control"
    )["targetId"] = "linux-ubuntu-24.04-x86_64"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(wrong_control_target)
    with pytest.raises(RuntimeError, match="Ubuntu 24.04 Node 24 target"):
        module.validate_pin_shape(wrong_control_target)


def test_catalyst_binding_gate_matches_catalog_package_and_active_uds_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selected Catalog bindings must match Platform UDS active installation readback."""
    acceptance_root = tmp_path / "acceptance"
    (acceptance_root / "downloads").mkdir(parents=True)
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    module = _load_driver_module(tmp_path, "acceptance_driver_bindings")
    pins = _valid_pins()
    catalog = _verified_catalyst_catalog()
    catalog_bytes = json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode()
    catalog_pin = pins["catalog"]
    catalog_pin["sha256"] = hashlib.sha256(catalog_bytes).hexdigest()
    catalog_pin["sizeBytes"] = len(catalog_bytes)
    pins["workloads"]["catalyst"]["catalogDigest"] = "sha256:" + catalog_pin["sha256"]
    module.write_json(acceptance_root / "release-pins-v1.json", pins)
    (acceptance_root / "downloads" / catalog_pin["assetName"]).write_bytes(catalog_bytes)

    expected = module.expected_catalyst_bindings(
        pins["workloads"]["catalyst"]["selectedComponents"]
    )
    assert len(expected) == 4
    assert {row["packageId"] for row in expected} == {
        "cyrene.tools.dataset-generation",
        "cyrene.tools.dataset-preparation",
        "cyrene.tools.document-parsing",
        "cyrene.tools.knowledge-preparation",
    }
    observed = [
        {
            **row,
            "installationIds": [f"installation-{index:032x}"],
            "activeInstallationId": f"installation-{index:032x}",
            "state": "RUNNING",
            "failureCode": None,
        }
        for index, row in enumerate(expected, start=1)
    ]
    verified = module.assert_catalyst_source_bindings(observed, expected, "synthetic UDS readback")
    assert all(row["state"] == "RUNNING" for row in verified)
    with pytest.raises(RuntimeError, match="sourceBindings differ"):
        module.assert_catalyst_source_bindings(observed[:-1], expected, "synthetic UDS readback")
    with pytest.raises(RuntimeError, match="not running"):
        module.assert_catalyst_source_bindings(
            [{**observed[0], "state": "STOPPED"}, *observed[1:]],
            expected,
            "synthetic UDS readback",
        )


def test_client_web_http_gate_checks_live_contract_without_saving_session_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installed host gate compares live routes to receipts and withholds CSRF material."""
    acceptance_root = tmp_path / "acceptance"
    evidence_dir = acceptance_root / "evidence"
    evidence_dir.mkdir(parents=True)
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    module = _load_driver_module(tmp_path, "acceptance_driver_web")
    pins = _valid_pins()
    module.write_json(acceptance_root / "release-pins-v1.json", pins)
    index = b"<!doctype html><title>Client workspace</title>\n"
    index_digest = "sha256:" + hashlib.sha256(index).hexdigest()
    source_digest = "sha256:" + "a" * 64
    active_digest = "sha256:" + "b" * 64
    metadata = {
        "schemaVersion": 1,
        "componentId": "cyrene-client-workspace-web",
        "version": "1.0.0",
        "releaseIdentity": next(
            row["manifestDigest"]
            for row in pins["workloads"]["catalyst"]["selectedComponents"]
            if row["componentId"] == "cyrene-client-workspace-web"
        ),
        "sourceReceiptDigest": source_digest,
        "activeReceiptDigest": active_digest,
        "documentRoot": "/var/lib/cyrene/workloads/web/cyrene-client-workspace-web/current",
        "clientUrl": "http://127.0.0.1:8100/",
        "listenerAddress": "127.0.0.1",
        "listenerPort": 8100,
        "configPath": "/etc/cyrene/workloads/web/nginx.conf",
        "configDigest": "sha256:" + "c" * 64,
        "unitPath": "/etc/systemd/system/cyrene-workspace-web.service",
        "unitDigest": "sha256:" + "d" * 64,
        "serviceUnit": "cyrene-workspace-web.service",
        "serviceState": "active",
        "serviceEnabled": True,
        "nginxVersion": "nginx/1.26.0",
        "installed": True,
        "available": True,
        "probes": {
            "root": {"status": 200, "indexDigest": index_digest},
            "health": {"status": 200, "bodyStatus": "ok"},
            "controlReady": {"status": 200, "bodyStatus": "ready"},
            "localSession": {
                "status": 200,
                "authenticated": True,
                "state": "AUTHENTICATED",
                "refreshable": False,
            },
        },
    }
    receipt = {
        "components": [
            {
                "componentId": "cyrene-client-workspace-web",
                "immutableReceiptSha256": source_digest.removeprefix("sha256:"),
                "activeReceiptSha256": active_digest.removeprefix("sha256:"),
                "staticWeb": {"indexDigest": index_digest},
            }
        ]
    }
    module.write_json(evidence_dir / "installed-workload-receipt-identities.json", receipt)
    monkeypatch.setattr(
        module,
        "workload_request",
        lambda operation, **_fields: {
            "status": "ready",
            "catalogDigest": pins["workloads"]["catalyst"]["catalogDigest"],
            "hostMetadata": {"web": metadata},
        },
    )
    token = "0123456789abcdef" * 4
    session = {
        "authenticated": True,
        "state": "AUTHENTICATED",
        "sessionId": "local",
        "expiresAt": None,
        "refreshExpiresAt": None,
        "refreshable": False,
        "csrfToken": token,
        "refreshed": False,
    }
    requests: list[tuple[str, dict[str, str]]] = []

    def fake_loopback(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        requests.append((url, headers))
        if url == "http://127.0.0.1:8100/":
            return 200, {}, index
        if url == "http://127.0.0.1:8100/healthz":
            return 200, {}, b'{"status":"ok"}'
        if url == "http://127.0.0.1:5182/health/ready":
            return 200, {}, b'{"status":"ready"}'
        if url == "http://127.0.0.1:8100/api/v1/auth/session":
            return 200, {}, json.dumps(session).encode()
        raise AssertionError(f"unexpected network request: {url}")

    monkeypatch.setattr(module, "request_loopback", fake_loopback)
    result = module.client_web_http()
    assert [url for url, _headers in requests] == [
        "http://127.0.0.1:8100/",
        "http://127.0.0.1:8100/healthz",
        "http://127.0.0.1:5182/health/ready",
        "http://127.0.0.1:8100/api/v1/auth/session",
    ]
    assert all(headers.get("X-Studio-Control-Token") is None for _url, headers in requests)
    assert result["httpRoot"]["sha256"] == index_digest
    assert result["csrfTokenStored"] is False
    evidence_text = (evidence_dir / "client-web-http-acceptance.json").read_text()
    assert token not in evidence_text

    receipt["components"][0]["staticWeb"]["indexDigest"] = "sha256:" + "f" * 64
    module.write_json(evidence_dir / "installed-workload-receipt-identities.json", receipt)
    with pytest.raises(RuntimeError, match="HTTP root bytes differ"):
        module.client_web_http()


def test_catalyst_proxy_comparison_checks_status_and_json_shape_without_records(
    tmp_path: Path,
) -> None:
    """The Client proxy must match the Catalyst read schema without exporting rows."""
    module = _load_driver_module(tmp_path, "acceptance_driver_proxy")
    direct = json.dumps([{"id": "direct-private-row", "label": "Direct record"}]).encode()
    proxy = json.dumps([{"id": "proxy-private-row", "label": "Proxy record"}]).encode()

    result = module.compare_catalyst_read_responses(200, direct, 200, proxy)
    serialized = json.dumps(result)
    assert result["jsonSchemaMatches"] is True
    assert result["recordsStored"] is False
    assert "direct-private-row" not in serialized
    assert "proxy-private-row" not in serialized
    assert result["direct"]["bodySha256"] != result["proxy"]["bodySha256"]

    mismatched = json.dumps([{"id": "proxy-private-row", "title": "Proxy record"}]).encode()
    with pytest.raises(RuntimeError, match="response schema differs"):
        module.compare_catalyst_read_responses(200, direct, 200, mismatched)
    with pytest.raises(RuntimeError, match="returned HTTP 503"):
        module.compare_catalyst_read_responses(200, direct, 503, mismatched)


def test_workload_attestation_token_is_preserved_only_for_explicit_resolution_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only GitHub credentials are scoped to verified workload resolution."""
    _, driver_path = _workflow_driver(tmp_path)
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(tmp_path / "acceptance"))
    monkeypatch.setenv("GH_TOKEN", "synthetic-read-only-token")
    spec = importlib.util.spec_from_file_location("acceptance_driver_token", driver_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], *, input_text: str, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        request = json.loads(input_text)
        commands.append(command)
        envelope = {
            "protocolVersion": module.WORKLOAD_PROTOCOL,
            "operation": request["operation"],
            "ok": True,
            "result": {"status": "ready"},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(envelope) + "\n", "")

    monkeypatch.setattr(module, "run", fake_run)
    module.workload_request("check", preserve_read_token=True, workloadId="catalyst")
    module.workload_request("apply", workloadId="catalyst")

    assert "--preserve-env=GH_TOKEN" in commands[0]
    assert "--preserve-env=GH_TOKEN" not in commands[1]
    assert "synthetic-read-only-token" not in " ".join(commands[0] + commands[1])


def test_finalizer_fails_when_required_core_gates_are_not_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preflight-only or skipped core run cannot leave the workflow green."""
    _, driver_path = _workflow_driver(tmp_path)
    acceptance_root = tmp_path / "acceptance"
    acceptance_root.mkdir()
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    spec = importlib.util.spec_from_file_location("acceptance_driver_finalize", driver_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write_json(
        acceptance_root / "phase-ledger.json",
        {
            "schemaVersion": 1,
            "phases": {
                name: {
                    "status": "NOT_RUN",
                    "reason": "synthetic skipped gate",
                    "recordedAtUtc": "2026-01-01T00:00:00+00:00",
                    "evidence": None,
                }
                for name in module.PHASES
            },
        },
    )

    with pytest.raises(RuntimeError, match="required Catalyst core gates did not all pass"):
        module.finalize()

    ledger = json.loads((acceptance_root / "phase-ledger.json").read_text(encoding="utf-8"))
    assert ledger["requiredCorePass"] is False
    assert "native_static_binding_readback" in ledger["requiredCoreFailures"]
    assert "client_static_web_http" in ledger["requiredCoreFailures"]
    assert "client_release_integration" in ledger["requiredCoreFailures"]
