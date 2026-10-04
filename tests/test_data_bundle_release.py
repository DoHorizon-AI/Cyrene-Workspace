"""Fixed-input tests for the Workspace data-bundle release builder."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path, PurePosixPath

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = WORKSPACE_ROOT / "packaging/build_data_bundle_release.py"
SPEC = importlib.util.spec_from_file_location("data_bundle_release_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def test_policy_source_origin_is_distinct_from_bundle_asset_path(tmp_path: Path) -> None:
    """The unsigned fixture checks path mapping only; it is not a publication proof."""
    policy = b'{"schemaVersion":"cyrene.workspace.product.authorization-policy.v2"}\n'
    attestation_bundle = b"unsigned fixed fixture; no signature verification is claimed\n"
    policy_digest = release._digest(policy)
    bundle_digest = release._digest(attestation_bundle)
    source_commit = "a" * 40
    assets = tmp_path / "assets"
    _write(assets / release.POLICY_BUNDLE_PATH, policy)
    _write(assets / "workspace-product-policy-v2.attestation.jsonl", attestation_bundle)
    publication = {
        "source": {
            "repository": release.POLICY_REPOSITORY,
            "ref": "refs/heads/develop",
            "commit": source_commit,
        },
        "policy": {"path": release.POLICY_BUNDLE_PATH, "sha256": policy_digest},
        "provenance": {
            "bundlePath": "attestations/platform-policy.jsonl",
            "bundleSha256": bundle_digest,
            "subjectPath": f"{release.POLICY_REPOSITORY}/{release.POLICY_SOURCE_PATH}",
            "subjectDigest": policy_digest,
            "attestation": {
                "attestation": {
                    "kind": "github-artifact-attestation",
                    "subjectName": release.POLICY_BUNDLE_PATH,
                    "repository": release.POLICY_REPOSITORY,
                    "workflow": f"{release.POLICY_REPOSITORY}/.github/workflows/product-policy-release.yml",
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "run": {
                        "id": "123456",
                        "attempt": 1,
                        "url": f"https://github.com/{release.POLICY_REPOSITORY}/actions/runs/123456/attempts/1",
                    },
                }
            },
        },
    }
    publication_path = _write(
        tmp_path / "policy-publication.json",
        (json.dumps(publication, sort_keys=True) + "\n").encode(),
    )

    proof, files = release._policy_entry(publication_path, assets, "preview", source_commit)

    assert proof["path"] == release.POLICY_SOURCE_PATH
    assert proof["provenance"]["subjectPath"] == (
        f"{release.POLICY_REPOSITORY}/{release.POLICY_SOURCE_PATH}"
    )
    assert files == (
        (PurePosixPath(release.POLICY_BUNDLE_PATH), policy),
        (PurePosixPath("attestations/platform-policy.jsonl"), attestation_bundle),
    )


def test_policy_source_requires_the_catalogued_platform_origin(tmp_path: Path) -> None:
    """A source-origin mismatch remains rejected by the synthetic fixture."""
    policy = b"policy bytes\n"
    attestation_bundle = b"attestation bytes\n"
    policy_digest = release._digest(policy)
    bundle_digest = release._digest(attestation_bundle)
    source_commit = "b" * 40
    assets = tmp_path / "assets"
    _write(assets / release.POLICY_BUNDLE_PATH, policy)
    _write(assets / "workspace-product-policy-v2.attestation.jsonl", attestation_bundle)
    publication = {
        "source": {
            "repository": "DoHorizon-AI/Untrusted-Platform",
            "ref": "refs/heads/develop",
            "commit": source_commit,
        },
        "policy": {"path": release.POLICY_BUNDLE_PATH, "sha256": policy_digest},
        "provenance": {
            "bundlePath": "attestations/platform-policy.jsonl",
            "bundleSha256": bundle_digest,
            "subjectPath": f"{release.POLICY_REPOSITORY}/{release.POLICY_SOURCE_PATH}",
            "subjectDigest": policy_digest,
            "attestation": {},
        },
    }
    publication_path = _write(
        tmp_path / "policy-publication.json",
        (json.dumps(publication, sort_keys=True) + "\n").encode(),
    )

    with pytest.raises(release.ReleaseError, match="repository is not trusted"):
        release._policy_entry(publication_path, assets, "preview", source_commit)
