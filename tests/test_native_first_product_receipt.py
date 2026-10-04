"""
┌─────────────────────────────────────────────────────────────────────┐
│ Module: tests.test_native_first_product_receipt                      │
│ Role: Guard the signed-DEB-derived first Product cohort receipt.     │
│                                                                      │
│ 模块职责：验证首次 Product cohort receipt 的来源绑定和安全写入。      │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
ADMIN_PATH = (
    WORKSPACE_ROOT / "tooling" / "acceptance" / "native-components-v2" / "admin_initialize.py"
)
NATIVE_PATH = ADMIN_PATH.with_name("native_acceptance.py")
NATIVE_SPEC = importlib.util.spec_from_file_location("native_acceptance", NATIVE_PATH)
assert NATIVE_SPEC is not None and NATIVE_SPEC.loader is not None
native_acceptance = importlib.util.module_from_spec(NATIVE_SPEC)
sys.modules[NATIVE_SPEC.name] = native_acceptance
NATIVE_SPEC.loader.exec_module(native_acceptance)
ADMIN_SPEC = importlib.util.spec_from_file_location("native_first_product_admin_test", ADMIN_PATH)
assert ADMIN_SPEC is not None and ADMIN_SPEC.loader is not None
admin_initialize = importlib.util.module_from_spec(ADMIN_SPEC)
sys.modules[ADMIN_SPEC.name] = admin_initialize
ADMIN_SPEC.loader.exec_module(admin_initialize)

SOURCE_COMMIT = "a" * 40
DEB_DIGEST = "sha256:" + "b" * 64
PROFILE = "linux-ubuntu-24.04-x86_64-python-3.12"
PLATFORM_TARGET = "linux-ubuntu-24.04-x86_64-systemd"
SERVICES = ("navigator", "yield", "reactor", "exchange", "catalyst")


@pytest.fixture(autouse=True)
def _simulate_root_file_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model root-owned private-file metadata without executing as root."""

    monkeypatch.setattr(admin_initialize, "ADMIN_ROOT_UID", os.getuid())
    monkeypatch.setattr(admin_initialize, "ADMIN_ROOT_GID", os.getgid())
    monkeypatch.setattr(admin_initialize.os, "chown", lambda *_args: None)
    monkeypatch.setattr(admin_initialize.os, "fchown", lambda *_args: None)


def _sha256(payload: bytes) -> str:
    """Return a prefixed digest for receipt fixture bytes."""

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write_deb_fixture(root: Path) -> tuple[str, dict[str, Path]]:
    """Create five package-staged bundle trees and their pinned package index."""

    bundles: dict[str, Path] = {}
    services: dict[str, dict[str, Any]] = {}
    for service in SERVICES:
        component_id = f"cyrene-{service}"
        version = f"1.2.3-{service}"
        artifact_digest = "sha256:" + (str(len(service)) * 64)
        service_root = root / "usr/share/cyrene/service-artifacts" / service / version
        service_root.mkdir(parents=True)
        payload = f"signed staged payload:{service}".encode("ascii")
        (service_root / "payload.bin").write_bytes(payload)
        manifest = {
            "schema_version": 2,
            "service": service,
            "version": version,
            "source_commit": SOURCE_COMMIT,
            "artifact_digest": artifact_digest,
            "files": {"payload.bin": hashlib.sha256(payload).hexdigest()},
        }
        (service_root / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True), encoding="utf-8"
        )
        bundles[service] = service_root
        services[component_id] = {
            "componentId": component_id,
            "repository": f"DoHorizon-AI/Cyrene-{service.capitalize()}",
            "releaseId": "preview-" + SOURCE_COMMIT,
            "source": {"ref": "refs/heads/develop", "commit": SOURCE_COMMIT},
            "artifact": {"fixture": "officially-verified-by-parent-DEB-proof"},
            "manifest": {"fixture": "officially-verified-by-parent-DEB-proof"},
            "attestation": {"fixture": "officially-verified-by-parent-DEB-proof"},
        }
    index = {"schemaVersion": 1, "targetProfile": PROFILE, "services": services}
    index_bytes = json.dumps(index, sort_keys=True).encode("utf-8")
    index_path = root / "usr/share/cyrene/service-artifacts/index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_bytes(index_bytes)
    return hashlib.sha256(index_bytes).hexdigest(), bundles


def _mock_bundle_validator(
    bundle_path: Path, *, expected_service: str, expected_target_profile: str
) -> dict[str, Any]:
    """Verify the fixture payload like the existing service bundle validator does."""

    assert expected_target_profile == PROFILE
    manifest_bytes = (bundle_path / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["service"] == expected_service
    assert (
        hashlib.sha256((bundle_path / "payload.bin").read_bytes()).hexdigest()
        == manifest["files"]["payload.bin"]
    )
    return manifest


def _derive(root: Path, index_digest: str) -> dict[str, Any]:
    """Derive a cohort from fixture DEB bytes using the validator seam."""

    return admin_initialize._derive_first_product_cohort(
        root,
        deb_sha256=DEB_DIGEST,
        source_commit=SOURCE_COMMIT,
        target_id=PLATFORM_TARGET,
        target_profile=PROFILE,
        service_artifacts_index_sha256=index_digest,
        bundle_validator=_mock_bundle_validator,
    )


def test_cohort_binds_verified_deb_index_and_exact_staged_manifests(tmp_path: Path) -> None:
    """The receipt includes exactly five validated bundles in canonical order."""

    index_digest, bundles = _write_deb_fixture(tmp_path)
    receipt = _derive(tmp_path, index_digest)

    assert receipt["installer"] == {
        "debSha256": DEB_DIGEST,
        "sourceCommit": SOURCE_COMMIT,
        "targetId": PLATFORM_TARGET,
    }
    assert [product["service"] for product in receipt["products"]] == list(SERVICES)
    for product in receipt["products"]:
        manifest_bytes = (bundles[product["service"]] / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        assert product["version"] == manifest["version"]
        assert product["manifestDigest"] == _sha256(manifest_bytes)
        assert product["artifactDigest"] == manifest["artifact_digest"]
        assert product["bundlePath"].endswith(f"/{product['service']}/{manifest['version']}")
    material = {key: value for key, value in receipt.items() if key != "receiptDigest"}
    assert receipt["receiptDigest"] == _sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    )


def test_cohort_rejects_index_not_bound_to_verified_deb_proof(tmp_path: Path) -> None:
    """A caller-supplied index digest cannot replace the verified package proof."""

    index_digest, _bundles = _write_deb_fixture(tmp_path)
    with pytest.raises(admin_initialize.AdminInitializationError, match="differs from signed DEB"):
        _derive(tmp_path, "0" * 64)
    assert len(index_digest) == 64


def test_cohort_rejects_missing_product_and_changed_staged_bytes(tmp_path: Path) -> None:
    """Incomplete cohorts and bundle payload tampering fail closed."""

    index_digest, bundles = _write_deb_fixture(tmp_path)
    index_path = tmp_path / "usr/share/cyrene/service-artifacts/index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["services"].pop("cyrene-catalyst")
    index_bytes = json.dumps(index, sort_keys=True).encode("utf-8")
    index_path.write_bytes(index_bytes)
    with pytest.raises(admin_initialize.AdminInitializationError, match="exactly five"):
        _derive(tmp_path, hashlib.sha256(index_bytes).hexdigest())

    changed_root = tmp_path / "changed-deb"
    index_digest, bundles = _write_deb_fixture(changed_root)
    (bundles["yield"] / "payload.bin").write_bytes(b"changed after signing")
    with pytest.raises(AssertionError):
        _derive(changed_root, index_digest)


def test_receipt_write_is_root_private_idempotent_and_refuses_tampering(
    tmp_path: Path,
) -> None:
    """A fixed receipt path is created once and prior changed bytes are preserved."""

    index_digest, _bundles = _write_deb_fixture(tmp_path / "deb")
    receipt = _derive(tmp_path / "deb", index_digest)
    receipt_path = tmp_path / "var/lib/cyrene/native-initialization/first-product-cohort.json"
    first = admin_initialize._write_first_product_cohort_receipt(receipt, path=receipt_path)
    assert first["status"] == "written"
    info = receipt_path.stat()
    assert (info.st_uid, info.st_gid, info.st_mode & 0o777) == (
        os.getuid(),
        os.getgid(),
        0o600,
    )
    assert receipt_path.parent.stat().st_mode & 0o777 == 0o700
    second = admin_initialize._write_first_product_cohort_receipt(receipt, path=receipt_path)
    assert second["status"] == "verified-existing"

    tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
    tampered["products"][0]["version"] = "tampered"
    original_bytes = receipt_path.read_bytes()
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    tampered_bytes = receipt_path.read_bytes()
    with pytest.raises(admin_initialize.AdminInitializationError, match="invalid"):
        admin_initialize._write_first_product_cohort_receipt(receipt, path=receipt_path)
    assert receipt_path.read_bytes() == tampered_bytes
    assert original_bytes != tampered_bytes
