"""Static contract checks for the source-free modular clean-host workflow."""

from __future__ import annotations

import ast
import email.message
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import textwrap
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Self

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
                "channel": "preview",
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
                "channel": "preview",
                "service": {
                    "unit": "cyrene-catalyst.service",
                    "baseUrl": "http://127.0.0.1:8004",
                    "healthPath": "/healthz",
                },
                "selectedComponents": selected_components,
            }
        },
    }


def _with_exact_match_plugins(pins: dict[str, object]) -> dict[str, object]:
    """Add standalone exact-match and required SDK identity pins to the fixture."""
    catalyst = pins["workloads"]["catalyst"]
    rows = {row["componentId"]: row for row in catalyst["selectedComponents"]}
    exact_match = json.loads(json.dumps(rows["cyrene-tools-dataset-generation"]))
    exact_match.update(
        componentId="cyrene-evaluation-exact-match",
        targetId="linux-ubuntu-24.04-x86_64-python-3.12",
    )
    pins["workloads"]["plugins"] = {
        "workloadId": "plugins",
        "targetId": "linux-ubuntu-24.04-x86_64",
        "catalogDigest": catalyst["catalogDigest"],
        "channel": "preview",
        "selectedComponents": [
            rows["cyrene-runtime-maintenance-sdk"],
            exact_match,
        ],
    }
    return pins


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
    assert "MINIMUM_GH_ATTESTATION_VERSION = (2, 102, 0)" in source
    assert 'GH_RELEASE_TAG = "v2.102.0"' in source
    assert '"attestation_cli_provision"' in source
    assert '"sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends"' in source
    assert "https://github.com/cli/cli/releases/download/v2.102.0" in source
    assert "https://api.github.com/repos/cli/cli/releases/tags/v2.102.0" in source
    assert (
        'GH_RELEASE_ASSET_SHA256 = "7e54a307f90afdc59796c325ec0c49fb09e6c18537727207a8ac7513584ea5b0"'
        in source
    )
    assert '"ghVersion": ".".join(str(part) for part in gh_version)' in source
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
    assert 'origin + "/datasets"' in source
    assert '"datasetsRoute"' in source
    assert '"http://127.0.0.1:5182/health/ready"' in source
    assert '"/api/v1/auth/session"' in source
    assert 'status.get("hostMetadata", {}).get("web")' in source
    assert '"csrfTokenStored": False' in source
    assert '"sessionCommandPosted": False' in source
    assert '"/api/v1/catalyst/datasets"' in source
    assert '"/api/v1/datasets"' in source
    assert '"client_release_integration",' in source
    assert '"client_curation_proxy_mutation",' in source
    assert '"client_curation_browser_submission",' in source
    assert '"X-CSRF-Token":csrf' in source
    assert '"training-curation-v1"' in source
    assert 'phase("client_curation_proxy_mutation", client_curation_proxy_mutation)' in source
    assert '"retryAttempted":False' in source
    assert '"offline_retry_gate"' in source
    assert 'command.extend(["/usr/bin/unshare", "--net"])' in source
    assert 'error.get("code") == "NETWORK_ERROR"' in source
    assert '"same-plan repeated stage"' in source
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
    assert "official_catalog_release = github_release_metadata(" in source
    assert '"GitHub API URL is outside the fixed public HTTPS endpoint"' in source
    assert "no credential fallback was attempted" in source
    assert '"ssh-guest acceptance must not receive GitHub token environment variables"' in source
    assert '"--cert-oidc-issuer", "https://token.actions.githubusercontent.com"' in source
    assert "Runnable Catalyst core" in source


def test_anonymous_release_api_records_public_rate_limit_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSH guest release discovery uses one anonymous public API request."""
    acceptance_root = tmp_path / "acceptance"
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    monkeypatch.setenv("ACCEPTANCE_EXECUTION_MODE", "ssh-guest")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_ENTERPRISE_TOKEN", raising=False)
    module = _load_driver_module(tmp_path, "acceptance_driver_anonymous_api")
    requests: list[object] = []

    class Response:
        status = 200

        def __init__(self) -> None:
            self.headers = email.message.Message()
            self.headers["X-RateLimit-Remaining"] = "18"
            self.headers["X-RateLimit-Reset"] = "1791500000"

        def read(self, _limit: int) -> bytes:
            return b'{"tag_name":"preview-test","immutable":true}'

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Opener:
        def open(self, request: object, *, timeout: int) -> Response:
            assert timeout == 120
            requests.append(request)
            return Response()

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_args: Opener())

    result = module.github_api_json(
        "https://api.github.com/repos/DoHorizon-AI/Cyrene-Workspace/releases/tags/preview-test",
        label="test-release",
    )

    assert result["tag_name"] == "preview-test"
    assert len(requests) == 1
    assert requests[0].get_header("Authorization") is None
    evidence = json.loads(
        (acceptance_root / "evidence" / "github-http-readback.json").read_text(encoding="utf-8")
    )
    assert evidence["requests"] == [
        {
            "label": "test-release",
            "category": "release-metadata",
            "host": "api.github.com",
            "path": "/repos/DoHorizon-AI/Cyrene-Workspace/releases/tags/preview-test",
            "httpStatus": 200,
            "retryable": False,
            "authMode": "anonymous",
            "rateLimit": {
                "X-RateLimit-Limit": None,
                "X-RateLimit-Remaining": "18",
                "X-RateLimit-Used": None,
                "X-RateLimit-Reset": "1791500000",
                "X-RateLimit-Resource": None,
                "Retry-After": None,
                "X-GitHub-Request-Id": None,
            },
        }
    ]


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("mode", ["ssh-guest", "github-hosted"])
def test_github_http_auth_failure_is_retryable_without_credential_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    mode: str,
) -> None:
    """401/403 keeps rate-limit facts, drops bodies, and never retries with another credential."""
    acceptance_root = tmp_path / f"acceptance-{mode}-{status}"
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    monkeypatch.setenv("ACCEPTANCE_EXECUTION_MODE", mode)
    if mode == "ssh-guest":
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_ENTERPRISE_TOKEN", raising=False)
        expected_authorization = None
    else:
        monkeypatch.setenv("GH_TOKEN", "synthetic-explicit-read-token")
        expected_authorization = "Bearer synthetic-explicit-read-token"
    module = _load_driver_module(tmp_path, f"acceptance_driver_failure_{mode}_{status}")
    requests: list[object] = []

    class Opener:
        def open(self, request: object, *, timeout: int) -> object:
            assert timeout == 120
            requests.append(request)
            headers = email.message.Message()
            headers["X-RateLimit-Remaining"] = "0"
            headers["X-RateLimit-Reset"] = "1791501234"
            headers["Retry-After"] = "30"
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                "public body contains synthetic-explicit-read-token",
                headers,
                io.BytesIO(b"private response body must not be retained"),
            )

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_args: Opener())

    with pytest.raises(module.GitHubHttpFailure) as failure:
        module.github_api_json(
            "https://api.github.com/repos/DoHorizon-AI/Cyrene-Workspace/releases/tags/preview-test",
            label="test-auth-failure",
        )

    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == expected_authorization
    assert "no credential fallback was attempted" in str(failure.value)
    assert "private response body" not in str(failure.value)
    assert "synthetic-explicit-read-token" not in str(failure.value)
    assert failure.value.safe_evidence["httpStatus"] == status
    assert failure.value.safe_evidence["retryable"] is True
    assert failure.value.safe_evidence["rateLimit"]["X-RateLimit-Remaining"] == "0"
    assert failure.value.safe_evidence["rateLimit"]["X-RateLimit-Reset"] == "1791501234"
    serialized = json.dumps(
        json.loads(
            (acceptance_root / "evidence" / "github-http-readback.json").read_text(encoding="utf-8")
        )
    )
    assert f'"httpStatus": {status}' in serialized
    assert '"retryable": true' in serialized
    assert "private response body" not in serialized
    assert "synthetic-explicit-read-token" not in serialized


@pytest.mark.parametrize("name", ["GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"])
def test_ssh_guest_rejects_any_github_token_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Manual guest execution cannot silently inherit any supported GitHub token env."""
    monkeypatch.setenv("ACCEPTANCE_EXECUTION_MODE", "ssh-guest")
    monkeypatch.setenv(name, "synthetic-token-must-not-be-used")
    module = _load_driver_module(tmp_path, f"acceptance_driver_guest_token_{name.lower()}")

    with pytest.raises(RuntimeError, match="ssh-guest acceptance must not receive"):
        module.github_api_token()


def test_release_asset_download_is_anonymous_and_digest_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release assets are fetched without Authorization and checked against API bytes."""
    acceptance_root = tmp_path / "acceptance"
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    monkeypatch.setenv("ACCEPTANCE_EXECUTION_MODE", "github-hosted")
    monkeypatch.setenv("GH_TOKEN", "synthetic-read-only-token")
    module = _load_driver_module(tmp_path, "acceptance_driver_asset_anonymous")
    payload = b"official release fixture\n"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    tag = "preview-test"
    repository = "DoHorizon-AI/Cyrene-Workspace"
    asset_name = "fixture.txt"
    asset = {
        "name": asset_name,
        "size": len(payload),
        "digest": digest,
        "browser_download_url": f"https://github.com/{repository}/releases/download/{tag}/{asset_name}",
    }
    requests: list[object] = []

    class Response:
        status = 200

        def __init__(self) -> None:
            self.stream = io.BytesIO(payload)

        def read(self, limit: int) -> bytes:
            return self.stream.read(limit)

        def geturl(self) -> str:
            return "https://release-assets.githubusercontent.com/fixture"

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Opener:
        def open(self, request: object, *, timeout: int) -> Response:
            assert timeout == 1800
            requests.append(request)
            return Response()

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_args: Opener())
    destination = tmp_path / "downloads"

    result = module.download_github_release_asset(
        asset,
        destination,
        repository=repository,
        release_tag=tag,
        label="test-asset",
    )

    assert len(requests) == 1
    assert requests[0].get_header("Authorization") is None
    assert (destination / asset_name).read_bytes() == payload
    assert result["sha256"] == digest
    assert result["sizeBytes"] == len(payload)
    assert result["authMode"] == "anonymous-public-asset"


def test_ssh_guest_runner_identity_records_driver_and_real_guest_without_workflow_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSH guest evidence identifies the driver source and leaves hosted workflow fields null."""
    acceptance_root = tmp_path / "acceptance"
    (acceptance_root / "evidence").mkdir(parents=True)
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    monkeypatch.setenv("ACCEPTANCE_EXECUTION_MODE", "ssh-guest")
    monkeypatch.setenv("ACCEPTANCE_DRIVER_SOURCE_REPOSITORY", "DoHorizon-AI/Cyrene-Workspace")
    monkeypatch.setenv("ACCEPTANCE_DRIVER_SOURCE_REF", "refs/heads/develop")
    monkeypatch.setenv("ACCEPTANCE_DRIVER_SOURCE_COMMIT", "a" * 40)
    for variable in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_REPOSITORY",
        "GITHUB_REF",
        "GITHUB_WORKFLOW_REF",
        "GITHUB_WORKFLOW_SHA",
    ):
        monkeypatch.delenv(variable, raising=False)
    module = _load_driver_module(tmp_path, "acceptance_driver_ssh_guest_identity")
    monkeypatch.setattr(
        module,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            "gh version 2.102.0\n" if command == ["gh", "--version"] else "",
            "",
        ),
    )
    monkeypatch.setattr(
        module,
        "trusted_prefix_lstat",
        lambda: {"method": "os.lstat", "followedSymlinks": False, "entries": []},
    )
    original_read_text = Path.read_text

    def read_guest_identity(path: Path, *args: object, **kwargs: object) -> str:
        if str(path) == "/etc/os-release":
            return 'ID=ubuntu\nVERSION_ID="24.04"\n'
        if str(path) == "/proc/1/comm":
            return "systemd\n"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_guest_identity)

    result = module.runner_identity()

    assert result["executionMode"] == "ssh-guest"
    assert result["driverSource"] == {
        "repository": "DoHorizon-AI/Cyrene-Workspace",
        "ref": "refs/heads/develop",
        "commit": "a" * 40,
        "workflowPath": ".github/workflows/modular-distribution-acceptance.yml",
        "driverScriptSha256": hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),
    }
    assert result["workflowRepository"] is None
    assert result["workflowGitRef"] is None
    assert result["workflowSha"] is None
    assert result["uid"] == os.getuid()
    assert result["gid"] == os.getgid()
    assert (
        json.loads(
            (acceptance_root / "evidence" / "runner-identity.json").read_text(encoding="utf-8")
        )
        == result
    )


def test_echo_storage_probe_compares_nested_secret_identity_without_retaining_raw_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real probe shape keeps internal hashes nested and withholds child output."""
    owner_uid = os.geteuid()
    owner_gid = os.getegid()
    assert owner_uid > 0
    root = tmp_path / "echo"
    data = root / "data"
    artifacts = data / "artifacts"
    data.mkdir(parents=True)
    artifacts.mkdir()
    (data / "state.json").write_text('{"fixture":true}\n', encoding="utf-8")
    source_token = root / "source-token"
    api_bearer = root / "api-bearer"
    source_token.write_bytes(b"source-token-fixture\n")
    source_token.chmod(stat.S_IRUSR)
    api_bearer.write_bytes(b"api-bearer-fixture\n")
    api_bearer.chmod(stat.S_IRUSR | stat.S_IWUSR)
    staged_root = root / "staged"
    staged = staged_root / "echo-test" / "activity-token"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"staged-token-fixture\n")
    staged.chmod(stat.S_IRUSR)
    host = {
        "hostUid": owner_uid,
        "hostGid": owner_gid,
        "dataDirectory": str(data),
        "artifactDirectory": str(artifacts),
        "containerName": "echo-test",
    }
    module = _load_driver_module(tmp_path, "acceptance_driver_echo_storage_probe")
    observed_calls: list[dict[str, object]] = []

    def run_fake_probe(
        command: list[str], *, label: str, input_text: str | None = None, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert label == "echo-persistent-data-and-credential-probe"
        assert kwargs.get("retain_output") is False
        assert input_text is not None
        for secret in ("source-token-fixture", "api-bearer-fixture", "staged-token-fixture"):
            assert secret not in input_text
        code = command[-1]
        path_replacements = {
            '"/var/lib/cyrene/echo"': json.dumps(str(data)),
            '"/var/lib/cyrene/echo/artifacts"': json.dumps(str(artifacts)),
            '"/etc/cyrene/runtime-activity-source-tokens/cyrene-echo.token"': json.dumps(
                str(source_token)
            ),
            '"/etc/cyrene/secrets/catalyst-api-token"': json.dumps(str(api_bearer)),
            '"/var/lib/cyrene/runtime-activity-source-tokens/oci"': json.dumps(str(staged_root)),
            "[(0,0,0o400)]": "[(os.geteuid(),os.getegid(),0o400)]",
            'cyrene=pwd.getpwnam("cyrene")': (
                'cyrene=type("CyreneIdentity",(),{"pw_uid":os.geteuid(),"pw_gid":os.getegid()})()'
            ),
        }
        for original, replacement in path_replacements.items():
            assert original in code
            code = code.replace(original, replacement)
        child = subprocess.run(
            [sys.executable, "-s", "-c", code],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        observed_calls.append(
            {"retainOutput": kwargs.get("retain_output"), "returnCode": child.returncode}
        )
        if child.returncode != 0:
            raise RuntimeError("Echo probe failed; raw output withheld")
        return child

    monkeypatch.setattr(module, "run", run_fake_probe)

    before = module.echo_storage_probe(host)
    assert (
        before["sourceToken"]["sha256Internal"]
        == hashlib.sha256(b"source-token-fixture").hexdigest()
    )
    assert (
        before["apiBearer"]["sha256Internal"] == hashlib.sha256(b"api-bearer-fixture").hexdigest()
    )

    (artifacts / "evaluation-fixture.zip").write_bytes(b"synthetic-evaluation-artifact")
    after_evaluation = module.echo_storage_probe(host)
    staged.unlink()
    after_uninstall = module.echo_storage_probe(host, expected=after_evaluation)

    assert (
        after_uninstall["sourceToken"]["sha256Internal"] == before["sourceToken"]["sha256Internal"]
    )
    assert (
        after_uninstall["apiBearer"]["sha256Internal"]
        == after_evaluation["apiBearer"]["sha256Internal"]
    )
    assert all(call["retainOutput"] is False and call["returnCode"] == 0 for call in observed_calls)
    wrong_expected = json.loads(json.dumps(after_evaluation))
    wrong_expected["sourceToken"]["sha256Internal"] = "0" * 64
    with pytest.raises(RuntimeError, match="raw output withheld"):
        module.echo_storage_probe(host, expected=wrong_expected)


def test_echo_uninstall_retention_uses_post_evaluation_artifact_baseline(tmp_path: Path) -> None:
    """Uninstall must preserve the new API artifact, not compare to the older tree."""
    module = _load_driver_module(tmp_path, "acceptance_driver_echo_artifact_retention")
    before = {"artifacts": {"fileCount": 1, "treeSha256": "before"}}
    after_evaluation = {"artifacts": {"fileCount": 2, "treeSha256": "after-evaluation"}}

    evidence = module.echo_artifact_retention_readback(
        before,
        after_evaluation,
        {"artifacts": {"fileCount": 2, "treeSha256": "after-evaluation"}},
    )

    assert evidence["evaluationArtifactAdded"] is True
    assert evidence["evaluationArtifactsPreserved"] is True
    assert evidence["filesAfterUninstall"] == 2
    with pytest.raises(RuntimeError, match="not preserved"):
        module.echo_artifact_retention_readback(
            before,
            after_evaluation,
            {"artifacts": {"fileCount": 1, "treeSha256": "before"}},
        )


def test_trusted_prefix_lstat_records_owner_mode_and_does_not_follow_symlinks(
    tmp_path: Path,
) -> None:
    """Prefix diagnostics use lstat so a link is not misreported as its target."""
    module = _load_driver_module(tmp_path, "acceptance_driver_trusted_prefix_lstat")
    directory = tmp_path / "system-prefix"
    directory.mkdir()
    directory.chmod(0o755)
    link = tmp_path / "system-prefix-link"
    link.symlink_to(directory, target_is_directory=True)
    missing = tmp_path / "missing-prefix"

    result = module.trusted_prefix_lstat((str(directory), str(link), str(missing)))

    assert result["method"] == "os.lstat"
    assert result["followedSymlinks"] is False
    directory_entry, link_entry, missing_entry = result["entries"]
    assert directory_entry["entryType"] == "directory"
    assert directory_entry["uid"] == os.geteuid()
    assert directory_entry["gid"] == os.getegid()
    assert directory_entry["modeOctal"] == "0755"
    assert directory_entry["groupWorldWritable"] is False
    assert link_entry["entryType"] == "symlink"
    assert link_entry["symlinkTarget"] == str(directory)
    assert missing_entry == {
        "path": str(missing),
        "status": "unavailable",
        "errorType": "FileNotFoundError",
    }


def test_runner_identity_saves_trusted_prefix_lstat_before_native_deb_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-install runner evidence includes the fixed system-prefix metadata set."""
    acceptance_root = tmp_path / "acceptance"
    (acceptance_root / "evidence").mkdir(parents=True)
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    monkeypatch.setenv("GITHUB_REPOSITORY", "DoHorizon-AI/Cyrene-Workspace")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/develop")
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        "DoHorizon-AI/Cyrene-Workspace/.github/workflows/modular-distribution-acceptance.yml@refs/heads/develop",
    )
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", "a" * 40)
    module = _load_driver_module(tmp_path, "acceptance_driver_runner_prefix_evidence")
    prefixes = {
        "method": "os.lstat",
        "followedSymlinks": False,
        "capturePoint": "before native DEB installation",
        "entries": [
            {"path": path, "status": "lstat-succeeded"} for path in module.TRUSTED_PREFIX_PATHS
        ],
    }
    monkeypatch.setattr(module, "trusted_prefix_lstat", lambda: prefixes)
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        module.platform,
        "uname",
        lambda: SimpleNamespace(_asdict=lambda: {"system": "Linux", "release": "test-kernel"}),
    )
    original_read_text = Path.read_text

    def read_runner_identity_file(path: Path, *args: object, **kwargs: object) -> str:
        if str(path) == "/etc/os-release":
            return 'ID=ubuntu\nVERSION_ID="24.04"\n'
        if str(path) == "/proc/1/comm":
            return "systemd\n"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_runner_identity_file)

    def successful_probe(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == ["gh", "--version"]:
            return subprocess.CompletedProcess(command, 0, "gh version 2.102.0\n", "")
        assert command == ["sudo", "-n", "true"]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module, "run", successful_probe)

    result = module.runner_identity()

    saved = json.loads((acceptance_root / "evidence" / "runner-identity.json").read_text())
    assert module.TRUSTED_PREFIX_PATHS == (
        "/usr",
        "/usr/share",
        "/usr/lib",
        "/opt",
        "/etc",
        "/var/lib",
    )
    assert result["trustedPrefixLstat"] == prefixes
    assert saved["trustedPrefixLstat"] == prefixes


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("gh version 2.102.0 (2026-09-01)", (2, 102, 0)),
        ("gh version 2.110.3 (2026-10-01)", (2, 110, 3)),
    ],
)
def test_attestation_cli_version_gate_accepts_supported_versions(
    tmp_path: Path, output: str, expected: tuple[int, int, int]
) -> None:
    """The acceptance runner records only a supported verifier version."""
    module = _load_driver_module(tmp_path, "acceptance_driver_gh_version")
    assert module.parse_gh_cli_version(output) == expected


@pytest.mark.parametrize(
    "output",
    [
        "gh version 2.97.0 (2026-07-31)",
        "GitHub CLI not installed",
    ],
)
def test_attestation_cli_version_gate_rejects_unsupported_versions(
    tmp_path: Path, output: str
) -> None:
    """Older or unparseable GitHub CLI versions fail before attestation checks."""
    module = _load_driver_module(tmp_path, "acceptance_driver_gh_version_reject")
    with pytest.raises(RuntimeError):
        module.parse_gh_cli_version(output)


def test_attestation_cli_keeps_an_already_supported_runner_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supported hosted runner avoids package installation and records its version."""
    acceptance_root = tmp_path / "acceptance"
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    module = _load_driver_module(tmp_path, "acceptance_driver_gh_already_supported")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        module.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None
    )

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert command == ["gh", "--version"]
        return subprocess.CompletedProcess(command, 0, "gh version 2.102.0 (2026-09-30)\n", "")

    monkeypatch.setattr(module, "run", fake_run)

    evidence = module.ensure_attestation_cli()

    assert evidence["versionBefore"] == "2.102.0"
    assert evidence["versionAfter"] == "2.102.0"
    assert evidence["upgraded"] is False
    assert commands == [["gh", "--version"]]
    assert (
        json.loads(
            (acceptance_root / "evidence" / "attestation-cli-provision.json").read_text(
                encoding="utf-8"
            )
        )
        == evidence
    )


def test_attestation_cli_bootstraps_missing_runner_from_exact_official_checksum_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old disposable runner verifies fixed official release bytes before apt install."""
    acceptance_root = tmp_path / "acceptance"
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    module = _load_driver_module(tmp_path, "acceptance_driver_gh_bootstrap")
    calls: list[list[str]] = []
    expected_deb_sha = "7e54a307f90afdc59796c325ec0c49fb09e6c18537727207a8ac7513584ea5b0"
    expected_checksums_sha = "afe49e9affa232faa8212aed035417166f6ade9b9470acb53d4dbd28c0504e8d"

    def fake_run(
        command: list[str], *, label: str, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command == ["gh", "--version"]:
            return subprocess.CompletedProcess(command, 0, "gh version 2.102.0\n", "")
        if command[0] == "dpkg-deb":
            return subprocess.CompletedProcess(
                command,
                0,
                "Package: gh\nVersion: 2.102.0\nArchitecture: amd64\n",
                "",
            )
        assert command[:4] == ["sudo", "-n", "apt-get", "install"]
        return subprocess.CompletedProcess(command, 0, "", "")

    release = {
        "tag_name": "v2.102.0",
        "immutable": True,
        "draft": False,
        "target_commitish": "fc4b137cdef0a6bd28fd461b7cf9c84a5812a8cd",
        "assets": [
            {
                "name": "gh_2.102.0_linux_amd64.deb",
                "digest": "sha256:" + expected_deb_sha,
                "size": 15392446,
                "browser_download_url": "https://github.com/cli/cli/releases/download/v2.102.0/gh_2.102.0_linux_amd64.deb",
            },
            {
                "name": "gh_2.102.0_checksums.txt",
                "digest": "sha256:" + expected_checksums_sha,
                "size": 1971,
                "browser_download_url": "https://github.com/cli/cli/releases/download/v2.102.0/gh_2.102.0_checksums.txt",
            },
        ],
    }

    def fake_release_metadata(*_args: object, **_kwargs: object) -> dict[str, object]:
        return release

    def fake_download(
        asset: dict[str, object], destination: Path, **_kwargs: object
    ) -> dict[str, object]:
        path = destination / str(asset["name"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.name.endswith(".deb"):
            with path.open("wb") as stream:
                stream.truncate(int(asset["size"]))
        else:
            path.write_text(expected_deb_sha + "  gh_2.102.0_linux_amd64.deb\n", encoding="utf-8")
        return {"assetName": path.name, "verifiedExistingBytes": False}

    def fake_sha256(path: Path) -> str:
        return expected_deb_sha if path.suffix == ".deb" else expected_checksums_sha

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "sha256", fake_sha256)
    monkeypatch.setattr(module, "github_release_metadata", fake_release_metadata)
    monkeypatch.setattr(module, "download_github_release_asset", fake_download)
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)

    evidence = module.ensure_attestation_cli()

    assert evidence["versionBefore"] == "not-installed"
    assert evidence["binaryPresentBefore"] is False
    assert evidence["versionAfter"] == "2.102.0"
    assert evidence["assetSha256"] == expected_deb_sha
    assert evidence["checksumsAssetSha256"] == expected_checksums_sha
    assert evidence["packageMetadata"] == {
        "name": "gh",
        "version": "2.102.0",
        "architecture": "amd64",
    }
    assert evidence["upgraded"] is True
    assert [command for command in calls if command == ["gh", "--version"]] == [["gh", "--version"]]
    apt_call = next(command for command in calls if command[:3] == ["sudo", "-n", "apt-get"])
    assert apt_call[-1].endswith("gh_2.102.0_linux_amd64.deb")


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

    wrong_workload_channel = json.loads(json.dumps(pins))
    wrong_workload_channel["workloads"]["catalyst"]["channel"] = "stable"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(wrong_workload_channel)
    with pytest.raises(RuntimeError, match="must explicitly pin the signed preview channel"):
        module.validate_pin_shape(wrong_workload_channel)

    wrong_index_channel = json.loads(json.dumps(pins))
    wrong_index_channel["workloads"]["catalyst"]["selectedComponents"][0]["indexIdentity"][
        "channel"
    ] = "stable"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(wrong_index_channel)
    with pytest.raises(RuntimeError, match="index channel differs from the workload plan channel"):
        module.validate_pin_shape(wrong_index_channel)

    catalyst_rows = {
        row["componentId"]: row for row in pins["workloads"]["catalyst"]["selectedComponents"]
    }
    echo_product = json.loads(json.dumps(catalyst_rows["cyrene-catalyst"]))
    echo_product.update(componentId="cyrene-echo", targetId="linux-ubuntu-24.04-x86_64-oci")
    echo_product["attestationRef"]["subjectName"] = "ghcr.io/example/cyrene-echo"
    echo_product["attestationRef"]["subjectDigest"] = echo_product["digest"]
    exact_match = dict(catalyst_rows["cyrene-tools-dataset-generation"])
    exact_match.update(
        componentId="cyrene-evaluation-exact-match",
        targetId="linux-ubuntu-24.04-x86_64-python-3.12",
    )
    pins["workloads"]["echo"] = {
        "workloadId": "echo",
        "targetId": "linux-ubuntu-24.04-x86_64",
        "catalogDigest": "sha256:" + "b" * 64,
        "channel": "preview",
        "selectedComponents": [
            echo_product,
            catalyst_rows["cyrene-runtime-maintenance-sdk"],
            exact_match,
        ],
    }
    jsonschema.Draft202012Validator(schema).validate(pins)
    module.validate_pin_shape(pins)

    missing_oci_subject = json.loads(json.dumps(pins))
    missing_oci_subject["workloads"]["echo"]["selectedComponents"][0]["attestationRef"].pop(
        "subjectName"
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(missing_oci_subject)
    with pytest.raises(RuntimeError, match="must name its attested image repository"):
        module.validate_pin_shape(missing_oci_subject)

    mismatched_oci_subject_digest = json.loads(json.dumps(pins))
    mismatched_oci_subject_digest["workloads"]["echo"]["selectedComponents"][0]["attestationRef"][
        "subjectDigest"
    ] = "sha256:" + "0" * 64
    with pytest.raises(RuntimeError, match="subject digest must equal the pinned image digest"):
        module.validate_pin_shape(mismatched_oci_subject_digest)

    acceptance_root = tmp_path / "echo-dispatch"
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    monkeypatch.setenv("RELEASE_PINS_JSON", json.dumps(pins))
    monkeypatch.setattr(module, "ensure_attestation_cli", lambda: {"testOnly": True})
    monkeypatch.setattr(module, "runner_identity", lambda: {"testOnly": True})

    module.init()

    ledger = json.loads((acceptance_root / "phase-ledger.json").read_text(encoding="utf-8"))
    assert ledger["phases"]["release_pins_validation"]["status"] == "PASS"
    assert ledger["phases"]["runner_identity"]["status"] == "PASS"
    assert ledger["phases"]["echo_exact_match_evaluate_uninstall"]["status"] == "NOT_RUN"
    saved_pins = json.loads((acceptance_root / "release-pins-v1.json").read_text(encoding="utf-8"))
    assert (
        saved_pins["workloads"]["echo"]["selectedComponents"]
        == pins["workloads"]["echo"]["selectedComponents"]
    )

    wrong_echo_workload_id = json.loads(json.dumps(pins))
    wrong_echo_workload_id["workloads"]["catalyst"]["workloadId"] = "echo"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(wrong_echo_workload_id)

    echo_with_service = json.loads(json.dumps(pins))
    echo_with_service["workloads"]["echo"]["service"] = {
        "unit": "cyrene-echo.service",
        "baseUrl": "http://127.0.0.1:8094",
        "healthPath": "/healthz",
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(echo_with_service)

    missing_exact_match = json.loads(json.dumps(pins))
    missing_exact_match["workloads"]["echo"]["selectedComponents"] = [
        row
        for row in missing_exact_match["workloads"]["echo"]["selectedComponents"]
        if row["componentId"] != "cyrene-evaluation-exact-match"
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(missing_exact_match)
    with pytest.raises(RuntimeError, match="exact-match plugin identities"):
        module.validate_pin_shape(missing_exact_match)

    plugins_pins = _with_exact_match_plugins(_valid_pins())
    jsonschema.Draft202012Validator(schema).validate(plugins_pins)
    module.validate_pin_shape(plugins_pins)

    index_metadata = json.loads(json.dumps(plugins_pins))
    first_component = index_metadata["workloads"]["catalyst"]["selectedComponents"][0]
    first_component["indexIdentity"].update(
        publisherIdentity=first_component["publisherIdentity"],
        assetUri="https://github.com/DoHorizon-AI/Cyrene-Catalyst/releases/download/preview/component-release-index-v1.json",
        channel="preview",
    )
    jsonschema.Draft202012Validator(schema).validate(index_metadata)
    module.validate_pin_shape(index_metadata)

    wrong_plugin_target = json.loads(json.dumps(plugins_pins))
    wrong_plugin_target["workloads"]["plugins"]["selectedComponents"][0]["targetId"] = (
        "linux-ubuntu-24.04-x86_64"
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(wrong_plugin_target)
    with pytest.raises(RuntimeError, match="standalone plugins pin target"):
        module.validate_pin_shape(wrong_plugin_target)

    extra_plugin = json.loads(json.dumps(plugins_pins))
    extra_plugin["workloads"]["plugins"]["selectedComponents"].append(
        extra_plugin["workloads"]["catalyst"]["selectedComponents"][0]
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(extra_plugin)

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


def test_standalone_plugins_pins_are_accepted_without_echo_install_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exact-match fallback pins do not enter Echo's unrelated OCI lifecycle gate."""
    acceptance_root = tmp_path / "plugins-dispatch"
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    pins = _with_exact_match_plugins(_valid_pins())
    monkeypatch.setenv("NATIVE_RELEASE_ID", pins["nativeInstaller"]["releaseId"])
    monkeypatch.setenv("RELEASE_PINS_JSON", json.dumps(pins))
    module = _load_driver_module(tmp_path, "acceptance_driver_plugins_pins")
    monkeypatch.setattr(module, "ensure_attestation_cli", lambda: {"versionAfter": "test-only"})
    monkeypatch.setattr(module, "runner_identity", lambda: {"testOnly": True})

    module.init()

    ledger = json.loads((acceptance_root / "phase-ledger.json").read_text(encoding="utf-8"))
    assert ledger["phases"]["release_pins_validation"]["status"] == "PASS"
    assert ledger["phases"]["echo_exact_match_evaluate_uninstall"]["status"] == "NOT_RUN"
    saved = json.loads((acceptance_root / "release-pins-v1.json").read_text(encoding="utf-8"))
    assert "plugins" in saved["workloads"]
    assert "echo" not in saved["workloads"]


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
        if url == "http://127.0.0.1:8100/datasets":
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
        "http://127.0.0.1:8100/datasets",
        "http://127.0.0.1:8100/healthz",
        "http://127.0.0.1:5182/health/ready",
        "http://127.0.0.1:8100/api/v1/auth/session",
    ]
    assert all(headers.get("X-Studio-Control-Token") is None for _url, headers in requests)
    assert result["httpRoot"]["sha256"] == index_digest
    assert result["datasetsRoute"] == {
        "path": "/datasets",
        "status": 200,
        "sha256": index_digest,
    }
    assert result["csrfTokenStored"] is False
    evidence_text = (evidence_dir / "client-web-http-acceptance.json").read_text()
    assert token not in evidence_text

    receipt["components"][0]["staticWeb"]["indexDigest"] = "sha256:" + "f" * 64
    module.write_json(evidence_dir / "installed-workload-receipt-identities.json", receipt)
    with pytest.raises(RuntimeError, match="HTTP root bytes differ"):
        module.client_web_http()


def test_client_curation_gate_submits_once_with_local_csrf_and_discards_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Client curation contract uses the fixed synthetic report and never saves credentials."""
    acceptance_root = tmp_path / "acceptance"
    evidence_dir = acceptance_root / "evidence"
    output_dir = acceptance_root / "catalyst-output"
    evidence_dir.mkdir(parents=True)
    output_dir.mkdir()
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    module = _load_driver_module(tmp_path, "acceptance_driver_client_curation")
    pins = _valid_pins()
    module.write_json(acceptance_root / "release-pins-v1.json", pins)
    dataset_id = "11111111-2222-4333-8444-555555555555"
    original_run_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    module.write_json(
        output_dir / "acceptance-report.json",
        {
            "status": "PASS",
            "fixture": "release-packaged authored business examples; no customer data",
            "fixtureVersion": "1",
            "datasetId": dataset_id,
            "curationRunId": original_run_id,
        },
    )
    csrf_token = "0123456789abcdef" * 4
    session = {
        "authenticated": True,
        "state": "AUTHENTICATED",
        "sessionId": "local",
        "refreshable": False,
        "csrfToken": csrf_token,
    }
    requests: list[tuple[str, dict[str, str]]] = []

    def fake_loopback(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        requests.append((url, headers))
        assert url == "http://127.0.0.1:8100/api/v1/auth/session"
        return 200, {}, json.dumps(session).encode()

    helper_result = {
        "datasetIdSha256": hashlib.sha256(dataset_id.encode()).hexdigest(),
        "originalCurationRunIdSha256": hashlib.sha256(original_run_id.encode()).hexdigest(),
        "newCurationRunIdSha256": "f" * 64,
        "sourceRevisionCount": 4,
        "sourcesProxyMatchesDirect": True,
        "originalRunProxyMatchesDirect": True,
        "clientMutation": {
            "method": "POST",
            "status": 202,
            "operation": "curateTrainingData",
            "path": "/api/v1/catalyst/api/v1/datasets/{datasetId}/processing-runs",
            "attempts": 1,
            "retryAttempted": False,
            "csrfHeaderPresent": True,
            "origin": "http://127.0.0.1:8100",
        },
        "runReadback": {
            "directStatus": 200,
            "proxyStatus": 200,
            "state": "SUCCEEDED",
            "jsonSchemaSha256": "1" * 64,
            "canonicalBodySha256": "2" * 64,
        },
        "recordsStored": False,
        "csrfTokenStored": False,
        "serverCredentialStored": False,
    }
    calls: list[dict[str, object]] = []

    def fake_run(
        command: list[str], *, label: str, input_text: str, retain_output: bool, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert label == "client-catalyst-curation-proxy-mutation"
        assert command[:4] == ["sudo", "-n", "python3", "-s"]
        assert retain_output is False
        assert len(command[-1]) > 1000
        assert '"X-CSRF-Token"' in command[-1]
        assert '"training-curation-v1"' in command[-1]
        assert '"operation":"curateTrainingData"' in command[-1]
        parsed_input = json.loads(input_text)
        assert parsed_input["datasetId"] == dataset_id
        assert parsed_input["curationRunId"] == original_run_id
        assert parsed_input["csrfToken"] == csrf_token
        calls.append(parsed_input)
        return subprocess.CompletedProcess(command, 0, json.dumps(helper_result), "")

    monkeypatch.setattr(module, "request_loopback", fake_loopback)
    monkeypatch.setattr(module, "run", fake_run)
    result = module.client_curation_proxy_mutation()

    assert len(requests) == 1
    assert requests[0][1]["Host"] == "127.0.0.1:8100"
    assert requests[0][1]["Origin"] == "http://127.0.0.1:8100"
    assert len(calls) == 1
    assert result["clientMutation"]["attempts"] == 1
    assert result["clientMutation"]["retryAttempted"] is False
    assert result["runReadback"]["state"] == "SUCCEEDED"
    evidence_text = (evidence_dir / "client-catalyst-curation-proxy-mutation.json").read_text()
    assert csrf_token not in evidence_text
    assert dataset_id not in evidence_text
    assert original_run_id not in evidence_text


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


@pytest.mark.parametrize("consumer_succeeds", [True, False])
def test_public_sft_fixture_is_staged_only_after_consumer_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer_succeeds: bool,
) -> None:
    """Only the exact ZIP accepted by the installed consumer enters uploadable evidence."""
    acceptance_root = tmp_path / "acceptance"
    acceptance_root.mkdir()
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(acceptance_root))
    monkeypatch.setenv("GH_TOKEN", "test-gh-token-must-not-reach-product")
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token-must-not-reach-product")
    module = _load_driver_module(tmp_path, "acceptance_driver_public_sft_fixture")
    module.write_json(acceptance_root / "release-pins-v1.json", _valid_pins())
    active_bundle = tmp_path / "installed-catalyst-release"
    active_bundle.mkdir()
    monkeypatch.setattr(
        module,
        "bundle_identity",
        lambda: {"activePath": str(active_bundle), "version": "pinned-version"},
    )
    monkeypatch.setattr(
        module,
        "request_http",
        lambda base_url, path: {
            "url": base_url + path,
            "status": 200,
            "contentType": "application/json",
        },
    )

    fixture_bytes = b"public-synthetic-sft-fixture\n"
    command_environments: list[dict[str, str]] = []

    def fake_run(
        command: list[str], *, label: str, env: dict[str, str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """Simulate the two installed commands while preserving fixture bytes."""
        command_environments.append(env)
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env
        if label == "installed-catalyst-acceptance-producer":
            output_directory = Path(command[command.index("--output-directory") + 1])
            output_directory.mkdir(parents=True, exist_ok=True)
            (output_directory / "authored-business-sft.zip").write_bytes(fixture_bytes)
            return subprocess.CompletedProcess(command, 0, "withheld product output", "")

        assert label == "installed-catalyst-bundle-consumer"
        assert Path(command[-1]).read_bytes() == fixture_bytes
        if not consumer_succeeds:
            raise RuntimeError("independent consumer rejected the fixture")
        return subprocess.CompletedProcess(command, 0, "withheld consumer output", "")

    monkeypatch.setattr(module, "run", fake_run)

    if consumer_succeeds:
        module.product_acceptance()
    else:
        with pytest.raises(RuntimeError, match="independent consumer rejected"):
            module.product_acceptance()

    published_fixture = (
        acceptance_root / "evidence" / "public-fixtures" / "authored-business-sft.zip"
    )
    identity_path = acceptance_root / "evidence" / "catalyst-generated-bundle-identity.json"
    assert published_fixture.is_file() is consumer_succeeds
    assert identity_path.is_file() is consumer_succeeds
    if consumer_succeeds:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        assert published_fixture.read_bytes() == fixture_bytes
        assert identity["artifactPath"] == "public-fixtures/authored-business-sft.zip"
        assert identity["consumerStatus"] == "PASS"
        assert identity["sha256"] == hashlib.sha256(fixture_bytes).hexdigest()
    assert all(
        "test-gh-token-must-not-reach-product" not in str(env) for env in command_environments
    )
    assert all(
        "test-github-token-must-not-reach-product" not in str(env) for env in command_environments
    )
    assert not (acceptance_root / "logs").exists()


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
    requests: list[dict[str, object]] = []

    def fake_run(
        command: list[str], *, input_text: str, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        request = json.loads(input_text)
        commands.append(command)
        requests.append(request)
        envelope = {
            "protocolVersion": module.WORKLOAD_PROTOCOL,
            "operation": request["operation"],
            "ok": True,
            "result": {"status": "ready"},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(envelope) + "\n", "")

    monkeypatch.setattr(module, "run", fake_run)
    module.workload_request(
        "check", preserve_read_token=True, workloadId="catalyst", channel="preview"
    )
    module.workload_request("apply", workloadId="catalyst", channel="preview")

    assert "--preserve-env=GH_TOKEN" in commands[0]
    assert "--preserve-env=GH_TOKEN" not in commands[1]
    assert "synthetic-read-only-token" not in " ".join(commands[0] + commands[1])
    assert [request["channel"] for request in requests] == ["preview", "preview"]


def test_ssh_guest_workload_resolution_does_not_preserve_or_require_github_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual guest resolution stays anonymous while retaining the installed JSONL protocol."""
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(tmp_path / "acceptance"))
    monkeypatch.setenv("ACCEPTANCE_EXECUTION_MODE", "ssh-guest")
    for variable in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    module = _load_driver_module(tmp_path, "acceptance_driver_guest_workload_request")
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], *, input_text: str, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        request = json.loads(input_text)
        envelope = {
            "protocolVersion": module.WORKLOAD_PROTOCOL,
            "operation": request["operation"],
            "ok": True,
            "result": {"status": "ready"},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(envelope) + "\n", "")

    monkeypatch.setattr(module, "run", fake_run)

    response = module.workload_request(
        "check",
        preserve_read_token=True,
        workloadId="catalyst",
        channel="preview",
    )

    assert response["status"] == "ready"
    assert commands == [["sudo", "-n", "/usr/bin/cyrene", "workload", "--json"]]


def test_network_isolated_workload_failure_uses_fixed_cli_and_sanitized_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One stage subprocess can lose network without changing host connectivity or exposing errors."""
    evidence_root = tmp_path / "acceptance"
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(evidence_root))
    monkeypatch.setenv("GH_TOKEN", "synthetic-read-only-token")
    module = _load_driver_module(tmp_path, "acceptance_driver_network_isolation")
    commands: list[list[str]] = []
    retained_output: list[object] = []
    request_line = '{"protocolVersion":"cyrene.workload-plan.v1","operation":"stage"}'
    failure = {
        "protocolVersion": module.WORKLOAD_PROTOCOL,
        "operation": "stage",
        "ok": False,
        "error": {
            "code": "NETWORK_ERROR",
            "message": "private detail synthetic-read-only-token",
            "retryable": True,
        },
    }

    def fake_run(
        command: list[str], *, input_text: str, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        retained_output.append(kwargs.get("retain_output"))
        assert input_text.strip() == request_line
        return subprocess.CompletedProcess(command, 0, json.dumps(failure) + "\n", "")

    monkeypatch.setattr(module, "run", fake_run)
    response = module.workload_request(
        "stage",
        preserve_read_token=True,
        network_isolated=True,
        allow_error=True,
    )

    assert response == failure
    assert retained_output == [False]
    assert commands[0][:6] == [
        "sudo",
        "-n",
        "--preserve-env=GH_TOKEN",
        "/usr/bin/unshare",
        "--net",
        "/usr/bin/cyrene",
    ]
    evidence = json.loads(
        (evidence_root / "evidence" / "workload-stage-01.json").read_text(encoding="utf-8")
    )
    serialized = json.dumps(evidence)
    assert evidence["response"]["error"] == {"code": "NETWORK_ERROR", "retryable": True}
    assert "message" not in evidence["response"]["error"]
    assert "synthetic-read-only-token" not in serialized


def test_offline_stage_failure_resumes_and_repeats_the_same_exact_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transient offline rejection must preserve one exact plan for same-plan retry."""
    evidence_root = tmp_path / "acceptance"
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(evidence_root))
    module = _load_driver_module(tmp_path, "acceptance_driver_offline_retry")
    pins = _valid_pins()
    workload = pins["workloads"]["catalyst"]
    checked = {
        "planId": "plan-" + "1" * 32,
        "planDigest": "sha256:" + "2" * 64,
        "channel": workload["channel"],
    }
    selected = workload["selectedComponents"]
    staged_rows = [{**row, "status": "staged"} for row in selected]
    staged_result = {
        "status": "staged",
        "planId": checked["planId"],
        "planDigest": checked["planDigest"],
        "catalogDigest": workload["catalogDigest"],
        "workloadId": "catalyst",
        "targetId": workload["targetId"],
        "channel": workload["channel"],
        "action": "install",
        "components": staged_rows,
    }
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_workload_request(operation: str, **fields: object) -> dict[str, object]:
        calls.append((operation, fields))
        if operation == "stage" and fields.get("network_isolated") is True:
            return {
                "protocolVersion": module.WORKLOAD_PROTOCOL,
                "ok": False,
                "operation": "stage",
                "error": {"code": "NETWORK_ERROR", "retryable": True},
            }
        if operation == "status":
            return {
                "status": "ready",
                "components": [
                    {"componentId": row["componentId"], "installed": False} for row in selected
                ],
            }
        assert operation == "stage"
        return staged_result

    monkeypatch.setattr(module, "workload_request", fake_workload_request)
    evidence = module.offline_stage_retry(workload, checked)

    stage_calls = [(operation, fields) for operation, fields in calls if operation == "stage"]
    assert len(stage_calls) == 3
    assert stage_calls[0][1]["network_isolated"] is True
    assert stage_calls[0][1]["allow_error"] is True
    assert all(
        fields["planId"] == checked["planId"]
        and fields["planDigest"] == checked["planDigest"]
        and fields["channel"] == workload["channel"]
        and fields["action"] == "install"
        for _, fields in stage_calls
    )
    assert [fields.get("network_isolated", False) for _, fields in stage_calls] == [
        True,
        False,
        False,
    ]
    assert evidence["activeSelectedComponentsAfterFailure"] == []
    assert evidence["failure"] == {
        "operation": "stage",
        "code": "NETWORK_ERROR",
        "retryable": True,
        "networkNamespace": "isolated-child-process",
    }
    assert evidence["repeatStageStable"] is True
    assert evidence["stagedResult"] == staged_result


def test_isolated_cached_stage_is_recorded_without_claiming_retry_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warm offline stage is useful cache evidence but does not prove failure recovery."""
    evidence_root = tmp_path / "acceptance"
    evidence_root.mkdir()
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(evidence_root))
    module = _load_driver_module(tmp_path, "acceptance_driver_cached_stage")
    pins = _valid_pins()
    module.write_json(evidence_root / "release-pins-v1.json", pins)
    workload = pins["workloads"]["catalyst"]
    checked = {
        "planId": "plan-" + "3" * 32,
        "planDigest": "sha256:" + "4" * 64,
        "channel": workload["channel"],
    }
    staged_result = {
        "status": "staged",
        "planId": checked["planId"],
        "planDigest": checked["planDigest"],
        "catalogDigest": workload["catalogDigest"],
        "workloadId": "catalyst",
        "targetId": workload["targetId"],
        "channel": workload["channel"],
        "action": "install",
        "components": [{**row, "status": "staged"} for row in workload["selectedComponents"]],
    }

    def fake_workload_request(operation: str, **fields: object) -> dict[str, object]:
        if operation == "status":
            return {
                "status": "ready",
                "components": [
                    {"componentId": row["componentId"], "installed": False}
                    for row in workload["selectedComponents"]
                ],
            }
        assert operation == "stage"
        if fields.get("network_isolated") is True:
            return {
                "protocolVersion": module.WORKLOAD_PROTOCOL,
                "operation": "stage",
                "ok": True,
                "result": staged_result,
            }
        return staged_result

    monkeypatch.setattr(module, "workload_request", fake_workload_request)
    result = module.record_offline_stage_outcome(workload, checked)

    assert result["outcome"] == "cached-stage-no-failure"
    assert result["repeatStageStable"] is True
    assert result["offlineFailureFallback"]["status"] == "NOT_RUN"
    assert result["offlineFailureFallback"]["outcome"] == "fallback-pins-unavailable"
    assert module.ledger()["phases"]["offline_retry_gate"]["status"] == "NOT_RUN"
    assert "cached content" in module.ledger()["phases"]["offline_retry_gate"]["reason"]


def test_cached_catalyst_stage_uses_independent_exact_match_plugin_retry_without_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugins fallback retries the same signed plan without Echo or mutation."""
    evidence_root = tmp_path / "acceptance"
    evidence_root.mkdir()
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(evidence_root))
    module = _load_driver_module(tmp_path, "acceptance_driver_exact_match_fallback")
    pins = _with_exact_match_plugins(_valid_pins())
    module.write_json(evidence_root / "release-pins-v1.json", pins)
    catalyst = pins["workloads"]["catalyst"]
    plugins = pins["workloads"]["plugins"]
    catalyst_check = {
        "planId": "plan-" + "3" * 32,
        "planDigest": "sha256:" + "4" * 64,
        "channel": catalyst["channel"],
    }
    plugin_check = {
        "status": "ready",
        "workloadId": "plugins",
        "targetId": plugins["targetId"],
        "channel": plugins["channel"],
        "action": "install",
        "catalogDigest": plugins["catalogDigest"],
        "planId": "plan-" + "5" * 32,
        "planDigest": "sha256:" + "6" * 64,
        "components": plugins["selectedComponents"],
        "resolution": {
            "catalogDigest": plugins["catalogDigest"],
            "channel": plugins["channel"],
            "planDigestMaterial": {"channel": plugins["channel"]},
            "selectedComponents": plugins["selectedComponents"],
        },
    }
    plugin_staged = {
        "status": "staged",
        "planId": plugin_check["planId"],
        "planDigest": plugin_check["planDigest"],
        "catalogDigest": plugins["catalogDigest"],
        "workloadId": "plugins",
        "targetId": plugins["targetId"],
        "channel": plugins["channel"],
        "action": "install",
        "components": [{**row, "status": "staged"} for row in plugins["selectedComponents"]],
    }
    catalyst_staged = {
        "status": "staged",
        "planId": catalyst_check["planId"],
        "planDigest": catalyst_check["planDigest"],
        "catalogDigest": catalyst["catalogDigest"],
        "workloadId": "catalyst",
        "targetId": catalyst["targetId"],
        "channel": catalyst["channel"],
        "action": "install",
        "components": [{**row, "status": "staged"} for row in catalyst["selectedComponents"]],
    }
    plugin_status = {
        "status": "ready",
        "catalogDigest": plugins["catalogDigest"],
        "components": [
            {
                "componentId": row["componentId"],
                "installed": False,
                "version": None,
                "releaseId": None,
                "targetId": None,
                "manifestDigest": None,
                "manifestAssetDigest": None,
                "digest": None,
                "installationId": None,
                "verification": {"identityAttested": False},
            }
            for row in plugins["selectedComponents"]
        ],
    }
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_workload_request(operation: str, **fields: object) -> dict[str, object]:
        calls.append((operation, fields))
        workload_id = fields.get("workloadId")
        if operation == "check":
            assert workload_id == "plugins"
            assert fields["selections"] == {
                "includeComponentIds": ["cyrene-evaluation-exact-match"],
                "excludeComponentIds": [],
                "choices": {},
            }
            return plugin_check
        if operation == "status":
            if workload_id == "plugins":
                return plugin_status
            assert workload_id == "catalyst"
            return {
                "status": "ready",
                "components": [
                    {"componentId": row["componentId"], "installed": False}
                    for row in catalyst["selectedComponents"]
                ],
            }
        assert operation == "stage"
        if workload_id == "catalyst":
            if fields.get("network_isolated") is True:
                return {"ok": True, "result": catalyst_staged}
            return catalyst_staged
        if fields.get("network_isolated") is True:
            return {
                "protocolVersion": module.WORKLOAD_PROTOCOL,
                "ok": False,
                "operation": "stage",
                "error": {"code": "NETWORK_ERROR", "retryable": True},
            }
        return plugin_staged

    monkeypatch.setattr(module, "workload_request", fake_workload_request)
    result = module.record_offline_stage_outcome(catalyst, catalyst_check)

    assert result["outcome"] == "cached-stage-no-failure"
    assert result["offlineFailureFallback"]["outcome"] == "network-failure-recovered"
    assert result["offlineFailureFallback"]["status"] == "PASS"
    assert result["offlineFailureFallback"]["applied"] is False
    assert module.ledger()["phases"]["offline_retry_gate"]["status"] == "PASS"
    plugin_stages = [
        fields
        for operation, fields in calls
        if operation == "stage" and fields.get("workloadId") == "plugins"
    ]
    assert [fields.get("network_isolated", False) for fields in plugin_stages] == [
        True,
        False,
        False,
    ]
    assert all(
        fields["planId"] == plugin_check["planId"]
        and fields["planDigest"] == plugin_check["planDigest"]
        and fields["channel"] == plugins["channel"]
        and fields["action"] == "install"
        for fields in plugin_stages
    )
    assert not any(operation == "apply" for operation, _ in calls)
    fallback_evidence = json.loads(
        (evidence_root / "evidence" / "exact-match-plugins-offline-stage.json").read_text(
            encoding="utf-8"
        )
    )
    serialized = json.dumps(fallback_evidence)
    assert "cyrene-echo" not in serialized
    assert fallback_evidence["failure"] == {
        "operation": "stage",
        "code": "NETWORK_ERROR",
        "retryable": True,
        "networkNamespace": "isolated-child-process",
    }


def test_exact_match_plugin_fallback_reports_cache_without_claiming_failure_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second warm cache remains NOT_RUN even when standalone pins are supplied."""
    evidence_root = tmp_path / "acceptance"
    evidence_root.mkdir()
    monkeypatch.setenv("ACCEPTANCE_ROOT", str(evidence_root))
    module = _load_driver_module(tmp_path, "acceptance_driver_exact_match_cached")
    pins = _with_exact_match_plugins(_valid_pins())
    module.write_json(evidence_root / "release-pins-v1.json", pins)
    catalyst = pins["workloads"]["catalyst"]
    plugins = pins["workloads"]["plugins"]
    catalyst_checked = {
        "planId": "plan-" + "3" * 32,
        "planDigest": "sha256:" + "4" * 64,
        "channel": catalyst["channel"],
    }
    plugin_checked = {
        "status": "ready",
        "workloadId": "plugins",
        "targetId": plugins["targetId"],
        "channel": plugins["channel"],
        "action": "install",
        "catalogDigest": plugins["catalogDigest"],
        "planId": "plan-" + "5" * 32,
        "planDigest": "sha256:" + "6" * 64,
        "components": plugins["selectedComponents"],
        "resolution": {
            "catalogDigest": plugins["catalogDigest"],
            "channel": plugins["channel"],
            "planDigestMaterial": {"channel": plugins["channel"]},
            "selectedComponents": plugins["selectedComponents"],
        },
    }
    staged_by_id = {}
    for workload_id, workload, checked in (
        ("catalyst", catalyst, catalyst_checked),
        ("plugins", plugins, plugin_checked),
    ):
        staged_by_id[workload_id] = {
            "status": "staged",
            "planId": checked["planId"],
            "planDigest": checked["planDigest"],
            "catalogDigest": workload["catalogDigest"],
            "workloadId": workload_id,
            "targetId": workload["targetId"],
            "channel": workload["channel"],
            "action": "install",
            "components": [{**row, "status": "staged"} for row in workload["selectedComponents"]],
        }

    def fake_workload_request(operation: str, **fields: object) -> dict[str, object]:
        workload_id = fields.get("workloadId")
        if operation == "check":
            assert workload_id == "plugins"
            return plugin_checked
        if operation == "status":
            workload = plugins if workload_id == "plugins" else catalyst
            return {
                "status": "ready",
                "catalogDigest": workload["catalogDigest"],
                "components": [
                    {
                        "componentId": row["componentId"],
                        "installed": False,
                        "version": None,
                        "releaseId": None,
                        "targetId": None,
                        "manifestDigest": None,
                        "manifestAssetDigest": None,
                        "digest": None,
                        "installationId": None,
                        "verification": {"identityAttested": False},
                    }
                    for row in workload["selectedComponents"]
                ],
            }
        assert operation == "stage"
        result = staged_by_id[str(workload_id)]
        return {"ok": True, "result": result} if fields.get("network_isolated") else result

    monkeypatch.setattr(module, "workload_request", fake_workload_request)
    result = module.record_offline_stage_outcome(catalyst, catalyst_checked)

    assert result["offlineFailureFallback"]["outcome"] == "cached-stage-no-failure"
    assert result["offlineFailureFallback"]["status"] == "NOT_RUN"
    assert module.ledger()["phases"]["offline_retry_gate"]["status"] == "NOT_RUN"


def test_version_conflict_gate_is_a_read_only_assertion_on_the_exact_plan(
    tmp_path: Path,
) -> None:
    """The intended release plan must be free of resolver version conflicts before staging."""
    module = _load_driver_module(tmp_path, "acceptance_driver_version_conflict")
    workload = _valid_pins()["workloads"]["catalyst"]
    checked = {
        "status": "ready",
        "planId": "plan-" + "5" * 32,
        "planDigest": "sha256:" + "6" * 64,
        "blockers": [],
        "resolution": {"blockers": []},
    }
    result = module.version_conflict_readonly_check(checked, workload)
    assert result["versionConflictBlockerCount"] == 0
    assert result["mutatingRequestsSent"] == 0

    checked["resolution"]["blockers"] = [{"code": "VERSION_CONFLICT"}]
    with pytest.raises(RuntimeError, match="VERSION_CONFLICT blocker"):
        module.version_conflict_readonly_check(checked, workload)


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
    assert "version_conflict_gate" in ledger["requiredCoreFailures"]
    assert "client_static_web_http" in ledger["requiredCoreFailures"]
    assert "client_release_integration" in ledger["requiredCoreFailures"]
    assert ledger["fullDistributionAcceptance"] == "INCOMPLETE"
    assert ledger["phase2Acceptance"] == "INCOMPLETE"
    assert "offline_retry_gate" in ledger["phase2IncompletePhases"]
    assert (
        "installer_interrupted_transaction_recovery" in ledger["fullDistributionIncompletePhases"]
    )
