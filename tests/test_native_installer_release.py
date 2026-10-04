"""Focused tests for exact source pins and offline native installer proofs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest
import yaml

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = WORKSPACE_ROOT / "scripts" / "native_installer_release.py"


def _module() -> ModuleType:
    name = "cyrene_native_installer_release_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _dispatch_inputs(module: ModuleType) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "nativeProfiles": list(module.PROFILE_IDS),
        "workspaceCatalog": {
            "releaseId": "catalog-preview-" + "a" * 40,
            "sha256": "b" * 64,
        },
        "platformReleaseId": "preview-" + "c" * 40,
        "productReleaseIds": {
            product: "preview-" + f"{index:x}" * 40
            for index, product in enumerate(module.PRODUCTS, start=1)
        },
    }


@pytest.mark.parametrize("schema_version", [1, 2])
def test_component_manifest_schema_accepts_only_supported_integer_versions(
    schema_version: int,
) -> None:
    module = _module()
    assert module._is_supported_component_manifest_schema(schema_version)


@pytest.mark.parametrize("schema_version", [3, "1", True, None])
def test_component_manifest_schema_rejects_unknown_and_non_integer_versions(
    schema_version: object,
) -> None:
    module = _module()
    assert not module._is_supported_component_manifest_schema(schema_version)


def test_fetch_plan_is_fixed_to_exact_ubuntu_tuples(tmp_path: Path) -> None:
    module = _module()
    inputs = tmp_path / "inputs.json"
    output = tmp_path / "fetch-plan.json"
    inputs.write_text(json.dumps(_dispatch_inputs(module)), encoding="utf-8")

    assert (
        module.main(
            [
                "write-fetch-plan",
                "--inputs",
                str(inputs),
                "--source-ref",
                "refs/heads/develop",
                "--source-commit",
                "d" * 40,
                "--output",
                str(output),
            ]
        )
        == 0
    )
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["channel"] == "preview"
    assert len(plan["requests"]) == 21
    assert {row["targetId"] for row in plan["requests"]} == {
        module.SDK_TARGET,
        *module.PLATFORM_TARGET_IDS.values(),
        *module.PROFILE_IDS,
    }
    assert sum(row["key"].startswith("product/") for row in plan["requests"]) == 10
    assert sum(row["key"].startswith("platform-core/") for row in plan["requests"]) == 10
    assert all(row["releaseId"].startswith("preview-") for row in plan["requests"])


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(platformReleaseId="latest"), "exact preview"),
        (
            lambda value: value.update(nativeProfiles=[value["nativeProfiles"][1]]),
            "canonical order",
        ),
        (lambda value: value["productReleaseIds"].pop("yield"), "exactly the five"),
    ],
)
def test_dispatch_inputs_reject_unpinned_or_incomplete_locators(
    tmp_path: Path, mutate: object, message: str
) -> None:
    module = _module()
    value = _dispatch_inputs(module)
    mutate(value)
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(module.ReleaseError, match=message):
        module._read_dispatch_inputs(path, "refs/heads/develop", "d" * 40)


@pytest.mark.parametrize("use_release_index_repository_id", [True, False])
def test_stage_service_artifacts_matches_catalog_repository_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_release_index_repository_id: bool,
) -> None:
    module = _module()
    profile = module.PROFILE_IDS[0]
    target = {
        "os": "linux",
        "osVersion": "22.04",
        "distribution": "ubuntu",
        "distributionVersion": "22.04",
        "architecture": "x86_64",
        "abi": "glibc-2.35",
        "runtime": "python:3.12",
    }
    catalog = {
        "components": [
            {
                "componentId": component_id,
                "targets": [{"targetId": profile, "support": "supported"}],
            }
            for _, component_id in module.PRODUCTS.values()
        ],
        "targets": [{"id": profile, "target": target}],
    }
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    reports: list[str] = []
    attestations: list[str] = []
    tuples = []

    for product, (repository, component_id) in module.PRODUCTS.items():
        key = f"product/{product}/22.04"
        release_id = "preview-" + hashlib.sha1(product.encode()).hexdigest()
        source = {"ref": "refs/heads/develop", "commit": release_id.removeprefix("preview-")}
        source_with_repository = {
            "repository": f"https://github.com/{repository}",
            **source,
        }
        artifact_name = f"{component_id}-{profile}.tar.gz"
        manifest_name = f"{component_id}-{profile}.manifest.json"
        row_dir = tmp_path / product
        row_dir.mkdir()
        artifact_path = row_dir / artifact_name
        artifact_bytes = f"verified archive bytes for {product}".encode()
        artifact_path.write_bytes(artifact_bytes)
        manifest = {
            "releaseId": release_id,
            "componentId": component_id,
            "source": source_with_repository,
            "target": target,
            "artifact": {
                "kind": "python-bundle",
                "format": "tar.gz",
                "sizeBytes": len(artifact_bytes),
            },
            "manifestDigest": "sha256:" + hashlib.sha256(product.encode()).hexdigest(),
        }
        manifest_path = row_dir / manifest_name
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        index = {
            "repository": (
                repository
                if use_release_index_repository_id
                else f"https://github.com/{repository}"
            ),
            "channel": "preview",
            "source": source_with_repository,
        }
        index_path = row_dir / "component-release-index-v1.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        attestation_path = row_dir / f"{artifact_name}.attestation.jsonl"
        attestation_path.write_text("verified bundle bytes", encoding="utf-8")
        report = {
            "artifactPath": str(artifact_path),
            "manifestPath": str(manifest_path),
            "manifest": manifest,
            "index": index,
        }
        report_path = row_dir / "fetch-report.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        reports.append(f"{key}={report_path}")
        attestations.append(f"{key}={attestation_path}")
        tuples.append(
            {
                "key": key,
                "repository": repository,
                "releaseId": release_id,
                "source": source,
                "artifact": {
                    "assetName": artifact_name,
                    "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                    "sizeBytes": len(artifact_bytes),
                    "kind": "python-bundle",
                    "format": "tar.gz",
                    "attestation": {
                        "assetName": attestation_path.name,
                        "sha256": module._sha256(attestation_path),
                        "repository": repository,
                        "workflow": f"{repository}/.github/workflows/component-release.yml",
                        "predicateType": module.PREDICATE_TYPE,
                        "subjectName": artifact_name,
                        "sourceRef": source["ref"],
                        "sourceCommit": source["commit"],
                    },
                },
                "manifest": {
                    "assetName": manifest_name,
                    "rawSha256": module._sha256(manifest_path),
                    "declaredDigest": manifest["manifestDigest"],
                },
                "index": {"rawSha256": module._sha256(index_path)},
            }
        )

    receipt_path = tmp_path / "source-receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "workspaceSource": {"ref": "refs/heads/develop"},
                "verifiedTuples": tuples,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_validate_source_receipt", lambda *_args, **_kwargs: None)
    output = tmp_path / "verified-output"
    arguments = argparse.Namespace(
        profile=profile,
        source_receipt=receipt_path,
        service_report=reports,
        component_attestation=attestations,
        output=output,
        catalog=catalog_path,
    )

    if use_release_index_repository_id:
        assert module._write_service_artifacts(arguments) == 0
        assert (output / "index.json").is_file()
    else:
        with pytest.raises(
            module.ReleaseError,
            match="Product artifact bytes differ from verified receipt for catalyst",
        ):
            module._write_service_artifacts(arguments)


def test_release_workflow_stages_platform_fetcher_schemas_with_catalog() -> None:
    workflow_path = WORKSPACE_ROOT / ".github/workflows/native-installer-release.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["resolve-inputs"]["steps"]
    catalog_step = next(
        step
        for step in steps
        if step.get("name") == "Fetch the explicitly selected Workspace catalog"
    )
    fetch_step = next(
        step
        for step in steps
        if step.get("name") == "Fetch every exact catalog-authorized component tuple"
    )

    catalog_script = catalog_step["run"]
    for schema in (
        "component-release-manifest-v1.schema.json",
        "component-release-manifest-v2.schema.json",
        "component-release-index-v1.schema.json",
    ):
        assert schema in catalog_script
    assert (
        'cp "governance/$schema" "$RUNNER_TEMP/native-installer-inputs/$schema"' in catalog_script
    )
    assert "native-installer-inputs/component-catalog-v1.json" in fetch_step["env"]["CATALOG_PATH"]


def test_installer_script_scan_allows_only_stage_bootstrap_and_unit_reload(tmp_path: Path) -> None:
    module = _module()
    control = tmp_path / "DEBIAN"
    control.mkdir()
    (control / "postinst").write_text(
        "#!/bin/sh\nset -eu\n"
        + "PRIVATE_PYTHON=/opt/cyrene/python/3.12.14/bin/python3.12\n"
        + "\"${PRIVATE_PYTHON}\" -I -c 'import sys; assert sys.version_info[:3] == (3, 12, 14)'\n"
        + "RUNTIME_META=\"$(stat -c '%u:%a' /opt/cyrene/python/3.12.14)\"\n"
        + "if ! id -u cyrene >/dev/null 2>&1; then useradd --system cyrene; fi\n"
        + 'install -d -o cyrene -g cyrene -m 750 "$path"\n'
        + 'echo "Cyrene package initialized in stage-only mode."\n'
        + "# This existing command validates and stages: /usr/bin/cyrene service-bootstrap\n"
        + _runtime_state_staging_block()
        + 'if ! "/usr/bin/cyrene" service-bootstrap; then exit 1; fi\n'
        + "systemctl daemon-reload\n",
        encoding="utf-8",
    )
    for name in ("prerm", "postrm"):
        (control / name).write_text("#!/bin/sh\nset -eu\nexit 0\n", encoding="utf-8")

    hashes = module._static_installer_activation_check(control)
    assert set(hashes) == {"postinst", "prerm", "postrm"}
    assert all(len(value) == 64 for value in hashes.values())

    (control / "postrm").write_text(
        "#!/bin/sh\nsystemctl daemon-reload && systemctl stop cyrene.service\n",
        encoding="utf-8",
    )
    with pytest.raises(module.ReleaseError, match="activation or legacy-runtime"):
        module._static_installer_activation_check(control)
    (control / "postrm").write_text("#!/bin/sh\ncyrene-broker initialize\n", encoding="utf-8")
    with pytest.raises(module.ReleaseError, match="activation or legacy-runtime"):
        module._static_installer_activation_check(control)

    (control / "postrm").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (control / "postinst").write_text(
        "#!/bin/sh\nset -eu\n"
        + _runtime_state_staging_block().replace(
            'install -d -o root -g cyrene-runtime-maintenance -m 2770 "$RUNTIME_STATE_DIR"',
            'chmod 700 "$RUNTIME_STATE_DIR"',
        )
        + "if ! /usr/bin/cyrene service-bootstrap; then exit 1; fi\n",
        encoding="utf-8",
    )
    with pytest.raises(module.ReleaseError, match="broker-state handling"):
        module._static_installer_activation_check(control)


def _runtime_state_staging_block() -> str:
    return """RUNTIME_STATE_DIR=/var/lib/cyrene/runtime
if ! getent group cyrene-runtime-maintenance >/dev/null 2>&1; then
    groupadd --system cyrene-runtime-maintenance
fi
AUTHORITY_GID="$(getent group cyrene-runtime-maintenance | cut -d: -f3)"
case "${AUTHORITY_GID}" in
    ''|*[!0-9]*)
        echo "ERROR: cyrene-runtime-maintenance group has no valid numeric gid." >&2
        exit 1
        ;;
esac
if [ -L "${RUNTIME_STATE_DIR}" ]; then
    echo "ERROR: ${RUNTIME_STATE_DIR} is a symlink; refusing to follow existing runtime state." >&2
    exit 1
elif [ ! -e "${RUNTIME_STATE_DIR}" ]; then
    install -d -o root -g cyrene-runtime-maintenance -m 2770 "$RUNTIME_STATE_DIR"
elif [ ! -d "${RUNTIME_STATE_DIR}" ]; then
    echo "ERROR: ${RUNTIME_STATE_DIR} exists but is not a directory." >&2
    exit 1
fi
RUNTIME_STATE_META="$(stat -c '%u:%g:%a' -- "$RUNTIME_STATE_DIR")"
if [ "${RUNTIME_STATE_META}" != "0:${AUTHORITY_GID}:2770" ]; then
    echo "ERROR: Refusing to repair existing runtime state in place (${RUNTIME_STATE_DIR}: ${RUNTIME_STATE_META})." >&2
    exit 1
fi
"""


@pytest.mark.parametrize(
    "command_line",
    [
        "CYRENE_STAGE=1 /usr/bin/cyrene service-bootstrap",
        "env CYRENE_STAGE=1 /usr/bin/cyrene service-bootstrap",
        'if ! "/usr/bin/cyrene" service-bootstrap --activate-missing; then exit 1; fi',
        'if ! "/usr/bin/cyrene" service-bootstrap; then exit 1; fi\n/usr/bin/cyrene status',
        'if ! "/usr/bin/cyrene service-bootstrap; then exit 1; fi',
    ],
)
def test_postinst_rejects_environment_wrappers_activation_and_extra_calls(
    tmp_path: Path, command_line: str
) -> None:
    module = _module()
    control = tmp_path / "DEBIAN"
    control.mkdir()
    (control / "postinst").write_text(
        "#!/bin/sh\nset -eu\n" + _runtime_state_staging_block() + command_line + "\n",
        encoding="utf-8",
    )
    for name in ("prerm", "postrm"):
        (control / name).write_text("#!/bin/sh\nset -eu\nexit 0\n", encoding="utf-8")

    with pytest.raises(module.ReleaseError):
        module._static_installer_activation_check(control)


def test_failed_draft_create_does_not_poll_by_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    calls: list[tuple[str, str]] = []

    def fake_api(method: str, url: str, **kwargs: object) -> tuple[int, object]:
        calls.append((method, url))
        if method == "GET":
            return 404, None
        return 502, None

    monkeypatch.setattr(module, "_api_json", fake_api)
    monkeypatch.setattr(module.time, "sleep", lambda _delay: None)
    with pytest.raises(module.ReleaseError, match="retry the workflow"):
        module._create_draft(
            "secret-token", "native-installer-stable-" + "a" * 40, "a" * 40, "stable"
        )

    assert len(calls) == 2
    assert calls[0][0] == "GET" and "/releases/tags/" in calls[0][1]
    assert calls[1][0] == "POST" and calls[1][1].endswith("/releases")


def _write_package_fixture(
    tmp_path: Path, module: ModuleType
) -> tuple[Path, dict[str, object], Path]:
    profile = module.PROFILE_IDS[0]
    target = {
        "os": "linux",
        "osVersion": "22.04",
        "distribution": "ubuntu",
        "distributionVersion": "22.04",
        "architecture": "x86_64",
        "abi": "glibc-2.35",
        "runtime": "python:3.12",
    }
    package_root = tmp_path / "package-root"
    raw_root = package_root / "usr/share/cyrene/verified-service-artifacts" / profile
    staged_root = package_root / "usr/share/cyrene/service-artifacts"
    control_root = package_root / "DEBIAN"
    raw_root.mkdir(parents=True)
    staged_root.mkdir(parents=True)
    control_root.mkdir()

    index_services: dict[str, object] = {}
    receipt_tuples = []
    for number, (product, (repository, component_id)) in enumerate(
        module.PRODUCTS.items(), start=1
    ):
        commit = f"{number:x}" * 40
        release_id = f"stable-{commit}"
        source_ref = "refs/heads/main"
        artifact_name = f"{component_id}-{profile}.tar.gz"
        manifest_name = (
            f"{component_id}-linux-ubuntu-22-04-x86-64-glibc-2-35-python-3-12.manifest.json"
        )
        bundle_name = f"{artifact_name}.attestation.jsonl"
        artifact_bytes = f"published-product-bytes-{product}".encode()
        bundle_bytes = f"signed-bundle-{product}".encode()
        manifest_digest = "sha256:" + f"{number + 1:x}" * 64
        row_root = raw_root / product
        row_root.mkdir()
        artifact_path = row_root / artifact_name
        manifest_path = row_root / manifest_name
        attestation_path = row_root / bundle_name
        artifact_path.write_bytes(artifact_bytes)
        attestation_path.write_bytes(bundle_bytes)

        service_source = {"ref": source_ref, "commit": commit}
        outer_manifest = {
            "schemaVersion": 1,
            "componentId": component_id,
            "releaseId": release_id,
            "version": "1.0.0",
            "channel": "stable",
            "target": target,
            "source": {
                "repository": f"https://github.com/{repository}",
                **service_source,
            },
            "artifact": {
                "kind": "python-bundle",
                "format": "tar.gz",
                "uri": (
                    f"https://github.com/{repository}/releases/download/{release_id}/{artifact_name}"
                ),
                "sha256": "sha256:" + hashlib.sha256(artifact_bytes).hexdigest(),
                "sizeBytes": len(artifact_bytes),
                "files": {
                    "service-bundle/manifest.json": "sha256:"
                    + hashlib.sha256(artifact_bytes).hexdigest()
                },
            },
            "dependencies": [],
            "restart": {"group": "none"},
            "provenance": {
                "attestation": {
                    "kind": "github-artifact-attestation",
                    "subjectName": artifact_name,
                    "repository": repository,
                    "workflow": f"{repository}/.github/workflows/component-release.yml",
                    "predicateType": module.PREDICATE_TYPE,
                    "run": {
                        "id": "1",
                        "attempt": 1,
                        "url": f"https://github.com/{repository}/actions/runs/1",
                    },
                }
            },
            "manifestDigest": manifest_digest,
        }
        manifest_bytes = (json.dumps(outer_manifest, sort_keys=True) + "\n").encode()
        manifest_path.write_bytes(manifest_bytes)
        artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        attestation_sha = hashlib.sha256(bundle_bytes).hexdigest()
        index_services[component_id] = {
            "componentId": component_id,
            "repository": repository,
            "releaseId": release_id,
            "source": service_source,
            "artifact": {
                "path": f"{product}/{artifact_name}",
                "sha256": artifact_sha,
                "sizeBytes": len(artifact_bytes),
                "kind": "python-bundle",
                "format": "tar.gz",
            },
            "manifest": {
                "path": f"{product}/{manifest_name}",
                "sha256": manifest_sha,
                "manifestDigest": manifest_digest,
            },
            "attestation": {
                "path": f"{product}/{bundle_name}",
                "sha256": attestation_sha,
                "repository": repository,
                "workflow": f"{repository}/.github/workflows/component-release.yml",
                "predicateType": module.PREDICATE_TYPE,
                "subjectName": artifact_name,
                "sourceRef": source_ref,
                "sourceCommit": commit,
            },
        }
        receipt_tuples.append(
            {
                "key": f"product/{product}/22.04",
                "repository": repository,
                "releaseId": release_id,
                "source": service_source,
                "componentId": component_id,
                "targetId": profile,
                "target": target,
                "artifactKind": "python-bundle",
                "artifact": {
                    "assetName": artifact_name,
                    "sha256": artifact_sha,
                    "sizeBytes": len(artifact_bytes),
                    "kind": "python-bundle",
                    "format": "tar.gz",
                    "attestation": {
                        "assetName": bundle_name,
                        "sha256": attestation_sha,
                        "repository": repository,
                        "workflow": f"{repository}/.github/workflows/component-release.yml",
                        "predicateType": module.PREDICATE_TYPE,
                        "subjectName": artifact_name,
                        "sourceRef": source_ref,
                        "sourceCommit": commit,
                    },
                },
                "manifest": {
                    "assetName": manifest_name,
                    "rawSha256": manifest_sha,
                    "declaredDigest": manifest_digest,
                },
            }
        )

    index = {"schemaVersion": 1, "targetProfile": profile, "services": index_services}
    index_bytes = (json.dumps(index, indent=2, sort_keys=True) + "\n").encode()
    (staged_root / "index.json").write_bytes(index_bytes)
    (raw_root / "index.json").write_bytes(index_bytes)

    scripts = {
        "postinst": (
            "#!/bin/sh\nset -eu\n"
            + "PRIVATE_PYTHON=/opt/cyrene/python/3.12.14/bin/python3.12\n"
            + "\"${PRIVATE_PYTHON}\" -I -c 'import sys; assert sys.version_info[:3] == (3, 12, 14)'\n"
            + "RUNTIME_META=\"$(stat -c '%u:%a' /opt/cyrene/python/3.12.14)\"\n"
            + "if ! id -u cyrene >/dev/null 2>&1; then useradd --system cyrene; fi\n"
            + 'install -d -o cyrene -g cyrene -m 750 "$path"\n'
            + 'echo "Cyrene package initialized in stage-only mode."\n'
            + "# This existing command validates and stages: /usr/bin/cyrene service-bootstrap\n"
            + _runtime_state_staging_block()
            + 'if ! "/usr/bin/cyrene" service-bootstrap; then exit 1; fi\n'
            + "systemctl daemon-reload\n"
        ),
        "prerm": "#!/bin/sh\nset -eu\nexit 0\n",
        "postrm": "#!/bin/sh\nset -eu\nexit 0\n",
    }
    for name, content in scripts.items():
        path = control_root / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
    packaged_scripts = package_root / "usr/lib/cyrene/scripts"
    packaged_scripts.mkdir(parents=True)
    shutil.copy2(WORKSPACE_ROOT / "cyrene", packaged_scripts / "cyrene.py")
    shutil.copy2(
        WORKSPACE_ROOT / "packaging/service_bundle.py",
        packaged_scripts / "service_bundle.py",
    )
    unit_root = package_root / "lib/systemd/system"
    unit_root.mkdir(parents=True)
    for _, component_id in module.PRODUCTS.values():
        (unit_root / f"{component_id}.service").write_text(
            "[Unit]\nDescription=Product\n"
            "Requires=cyrene-runtime-maintenance.service\n"
            "After=network.target cyrene-runtime-maintenance.service\n"
            "[Service]\nExecStart=/opt/cyrene/python/3.12.14/bin/python3.12\n",
            encoding="utf-8",
        )

    runtime_lock = json.loads(
        (WORKSPACE_ROOT / "packaging/python-runtime.lock.json").read_text(encoding="utf-8")
    )
    python_bytes = b"synthetic-private-cpython-executable"
    uv_bytes = b"synthetic-locked-uv-executable"
    python_archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=python_archive_buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("python/bin/python3.12")
        member.mode = 0o755
        member.uid = member.gid = 0
        member.size = len(python_bytes)
        archive.addfile(member, io.BytesIO(python_bytes))
    python_archive_bytes = python_archive_buffer.getvalue()
    runtime_lock["python"]["archive"]["sha256"] = hashlib.sha256(python_archive_bytes).hexdigest()
    runtime_lock["python"]["archive"]["size"] = len(python_archive_bytes)
    runtime_lock["buildResolver"]["binaryArchive"]["sha256"] = hashlib.sha256(
        b"synthetic-uv-archive"
    ).hexdigest()
    runtime_lock["buildResolver"]["binaryArchive"]["executableSha256"] = hashlib.sha256(
        uv_bytes
    ).hexdigest()
    requirements_bytes = b"synthetic-runtime-requirements-lock\n"
    runtime_lock["runtimeDependencies"]["requirementsSha256"] = hashlib.sha256(
        requirements_bytes
    ).hexdigest()
    wheel_bytes_by_name = {}
    for wheel in runtime_lock["runtimeDependencies"]["wheels"]:
        wheel_bytes = f"locked wheel {wheel['filename']}".encode()
        wheel["sha256"] = hashlib.sha256(wheel_bytes).hexdigest()
        wheel_bytes_by_name[wheel["filename"]] = wheel_bytes
    runtime_lock_bytes = (json.dumps(runtime_lock, sort_keys=True, indent=2) + "\n").encode()
    runtime_lock_sha = hashlib.sha256(runtime_lock_bytes).hexdigest()
    module.LOCKED_PYTHON_ARCHIVE_SHA256 = runtime_lock["python"]["archive"]["sha256"]
    module.LOCKED_UV_ARCHIVE_SHA256 = runtime_lock["buildResolver"]["binaryArchive"]["sha256"]
    module.LOCKED_UV_EXECUTABLE_SHA256 = runtime_lock["buildResolver"]["binaryArchive"][
        "executableSha256"
    ]

    runtime_payload = runtime_lock["payload"]
    python_archive_path = package_root / runtime_payload["runtimeArchivePath"]
    python_archive_path.parent.mkdir(parents=True, exist_ok=True)
    python_archive_path.write_bytes(python_archive_bytes)
    python_executable_path = package_root / runtime_lock["python"]["executable"].lstrip("/")
    python_executable_path.parent.mkdir(parents=True, exist_ok=True)
    python_executable_path.write_bytes(python_bytes)
    python_executable_path.chmod(0o755)
    uv_executable_path = package_root / runtime_lock["buildResolver"]["installedPath"].lstrip("/")
    uv_executable_path.parent.mkdir(parents=True, exist_ok=True)
    uv_executable_path.write_bytes(uv_bytes)
    uv_executable_path.chmod(0o755)
    embedded_lock_path = package_root / runtime_payload["lockPath"]
    embedded_lock_path.parent.mkdir(parents=True, exist_ok=True)
    embedded_lock_path.write_bytes(runtime_lock_bytes)
    verification_path = package_root / runtime_payload["verificationRecordPath"]
    verification_path.write_text(
        json.dumps(module._expected_python_runtime_record(runtime_lock), sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    requirements_path = package_root / runtime_payload["requirementsPath"]
    requirements_path.write_bytes(requirements_bytes)
    wheel_directory = package_root / runtime_payload["wheelDirectory"]
    wheel_directory.mkdir(parents=True)
    for filename, wheel_bytes in wheel_bytes_by_name.items():
        (wheel_directory / filename).write_bytes(wheel_bytes)
    (control_root / "control").write_text(
        "Package: cyrene\nVersion: 0.1.0\nArchitecture: amd64\n"
        "Maintainer: Cyrene Team <team@cyrene.dev>\nDescription: fixture\n",
        encoding="utf-8",
    )
    script_hashes = {
        name: hashlib.sha256((control_root / name).read_bytes()).hexdigest() for name in scripts
    }
    marker = {
        **module.INSTALL_CONTRACT_POLICY,
        "targetProfile": profile,
        "serviceArtifactsIndexSha256": hashlib.sha256(index_bytes).hexdigest(),
        "services": module._marker_services_from_index(
            index,
            profile,
            {
                "verifiedTuples": receipt_tuples,
            },
        ),
        "maintainerScriptsSha256": script_hashes,
    }
    marker_path = package_root / "usr/share/cyrene/native-install-contract-v1.json"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    deb_path = tmp_path / "fixture.deb"
    result = subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(package_root), str(deb_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    receipt = {
        "verifiedTuples": receipt_tuples,
        "pythonRuntimeLock": {
            "path": "packaging/python-runtime.lock.json",
            "sha256": runtime_lock_sha,
            "document": runtime_lock,
        },
        "workspaceSource": {
            "repository": module.REPOSITORY,
            "ref": "refs/heads/main",
            "commit": "d" * 40,
        },
    }
    return deb_path, receipt, package_root


def test_offline_deb_proof_checks_actual_marker_scripts_and_published_bytes(tmp_path: Path) -> None:
    if shutil.which("dpkg-deb") is None:
        pytest.skip("dpkg-deb is required for the DEB payload proof test")
    module = _module()
    deb_path, receipt, _package_root = _write_package_fixture(tmp_path, module)

    proof = module._inspect_deb_initialization(
        deb_path,
        module.PROFILE_IDS[0],
        receipt,
        verify_attestations=False,
        gh_executable="gh",
    )

    assert proof["checks"] == {
        "serviceActivation": "deferred",
        "brokerAction": "preserve-existing",
        "oldRuntimeAction": "preserve",
        "maintainerScriptsStaticScan": "passed",
        "verifiedServiceBytesPreserved": "passed",
        "freshBrokerUnavailable": "fail-closed",
        "upgradeState": "preserve-existing",
        "activeRuntimePointers": "preserve-existing",
        "pinnedPrivateRuntime": "passed",
    }
    assert set(proof["services"]) == {component_id for _, component_id in module.PRODUCTS.values()}
    assert proof["debSha256"] == hashlib.sha256(deb_path.read_bytes()).hexdigest()


def test_stage_only_verifier_derives_fail_closed_from_packaged_units(
    tmp_path: Path,
) -> None:
    module = _module()
    _, _, package_root = _write_package_fixture(tmp_path, module)
    unit = package_root / "lib/systemd/system/cyrene-catalyst.service"
    unit.write_text(
        unit.read_text(encoding="utf-8").replace(
            "Requires=cyrene-runtime-maintenance.service\n", ""
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.ReleaseError, match="require and follow the Platform broker"):
        module._validate_packaged_stage_only_behavior(package_root, module.PROFILE_IDS[0])


def test_stage_only_verifier_derives_pointer_preservation_from_packaged_code(
    tmp_path: Path,
) -> None:
    module = _module()
    _, _, package_root = _write_package_fixture(tmp_path, module)
    bundle_source = package_root / "usr/lib/cyrene/scripts/service_bundle.py"
    bundle_source.write_text(
        bundle_source.read_text(encoding="utf-8").replace("elif activate_missing:", "else:", 1),
        encoding="utf-8",
    )

    with pytest.raises(module.ReleaseError, match="guard its only activation"):
        module._validate_packaged_stage_only_behavior(package_root, module.PROFILE_IDS[0])
