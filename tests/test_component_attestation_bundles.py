"""Offline GitHub artifact-attestation consumption tests for component updates."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
UPDATES_PATH = WORKSPACE_ROOT / "packaging" / "component_updates.py"
spec = importlib.util.spec_from_file_location("component_attestation_updates_test", UPDATES_PATH)
assert spec is not None and spec.loader is not None
updates = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = updates
spec.loader.exec_module(updates)


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, url: str) -> None:
        super().__init__(payload)
        self.url = url

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _bundle_record(bundle: dict[str, object]) -> dict[str, object]:
    return {
        "repository_id": 123,
        "bundle_url": "https://attestations.example.invalid/bundle.json",
        "initiator": "github",
        "bundle": bundle,
    }


def _bundle_for(
    subject_name: str,
    digest: str,
    *,
    predicate_type: str = "https://slsa.dev/provenance/v1",
) -> dict[str, object]:
    statement = {
        "predicateType": predicate_type,
        "subject": [{"name": subject_name, "digest": {"sha256": digest.removeprefix("sha256:")}}],
    }
    encoded_statement = base64.b64encode(json.dumps(statement).encode()).decode()
    return {
        "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
        "verificationMaterial": {},
        "dsseEnvelope": {
            "payloadType": "application/vnd.in-toto+json",
            "payload": encoded_statement,
            "signatures": [],
        },
    }


def _verification_output(subject_name: str, digest: str) -> str:
    statement = {
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [{"name": subject_name, "digest": {"sha256": digest.removeprefix("sha256:")}}],
    }
    return json.dumps([{"verificationResult": {"statement": statement}}], separators=(",", ":"))


def _updater(
    tmp_path: Path,
    response: dict[str, object],
    runner,
) -> updates.ComponentUpdater:
    subject_digest = "sha256:" + hashlib.sha256(b"immutable signed subject bytes").hexdigest()
    api_uri = "https://api.github.com/repos/DoHorizon-AI/Cyrene-Platform/attestations/"
    api_uri += subject_digest + "?per_page=100"

    def opener(request, timeout):
        assert request.full_url == api_uri
        assert request.get_header("User-agent") == updates.USER_AGENT
        assert timeout == 30
        return _Response(json.dumps(response, separators=(",", ":")).encode(), request.full_url)

    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "generation": 1,
                "defaultChannel": "stable",
                "channels": {"stable": {}, "preview": {}},
                "components": [],
                "targets": [],
                "publishers": [],
            }
        ),
        encoding="utf-8",
    )
    return updates.ComponentUpdater(
        catalog_path=catalog,
        activity_catalog_path=tmp_path / "activity.json",
        state_root=tmp_path / "state",
        install_root=tmp_path / "install",
        trusted_catalog_digest=None,
        load_active_catalog=False,
        opener=opener,
        runner=runner,
    )


def test_public_bundle_is_passed_to_locked_verifier_in_private_temp_file(tmp_path: Path) -> None:
    payload = b"immutable signed subject bytes"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    bundle = _bundle_for("subject.tar.gz", digest)
    api_response = {"attestations": [_bundle_record(bundle)]}
    observed: list[dict[str, object]] = []

    def runner(arguments, **kwargs):
        bundle_index = arguments.index("--bundle") + 1
        bundle_path = Path(arguments[bundle_index])
        assert bundle_path.is_file() and not bundle_path.is_symlink()
        assert stat.S_IMODE(bundle_path.stat().st_mode) == 0o600
        assert json.loads(bundle_path.read_bytes()) == bundle
        assert arguments[arguments.index("--repo") + 1] == "DoHorizon-AI/Cyrene-Platform"
        assert (
            arguments[arguments.index("--signer-workflow") + 1]
            == "owner/repo/.github/workflows/release.yml"
        )
        assert arguments[arguments.index("--source-ref") + 1] == "refs/heads/develop"
        assert arguments[arguments.index("--source-digest") + 1] == "a" * 40
        assert (
            arguments[arguments.index("--predicate-type") + 1] == "https://slsa.dev/provenance/v1"
        )
        assert (
            arguments[arguments.index("--cert-oidc-issuer") + 1]
            == "https://token.actions.githubusercontent.com"
        )
        subject_path = Path(arguments[3])
        assert stat.S_IMODE(subject_path.stat().st_mode) == 0o600
        assert subject_path.read_bytes() == payload
        observed.append({"arguments": arguments, **kwargs})
        return SimpleNamespace(
            returncode=0, stdout=_verification_output("subject.tar.gz", digest), stderr=""
        )

    updater = _updater(tmp_path, api_response, runner)
    result = updater._release_attestation_bundle(
        payload=payload,
        repository="DoHorizon-AI/Cyrene-Platform",
        digest=digest,
        workflow="owner/repo/.github/workflows/release.yml",
        source_ref="refs/heads/develop",
        source_commit="a" * 40,
        subject_name="subject.tar.gz",
    )
    assert json.loads(result) == bundle
    assert len(observed) == 1


@pytest.mark.parametrize(
    ("response", "runner_result", "error_code"),
    [
        ({"attestations": []}, None, "ATTESTATION_MISSING"),
        ({"attestations": [_bundle_record({"unknown": True})]}, None, "INVALID_ATTESTATION"),
        (
            {
                "attestations": [
                    _bundle_record(
                        _bundle_for(
                            "subject.tar.gz",
                            "sha256:"
                            + hashlib.sha256(b"immutable signed subject bytes").hexdigest(),
                        )
                    )
                ]
            },
            SimpleNamespace(returncode=1, stdout="", stderr="invalid signature"),
            "ATTESTATION_INVALID",
        ),
        (
            {
                "attestations": [
                    _bundle_record(
                        _bundle_for(
                            "subject.tar.gz",
                            "sha256:"
                            + hashlib.sha256(b"immutable signed subject bytes").hexdigest(),
                            predicate_type="https://in-toto.io/attestation/release/v0.2",
                        )
                    )
                ]
            },
            None,
            "ATTESTATION_INVALID",
        ),
        (
            {
                "attestations": [
                    _bundle_record(
                        _bundle_for(
                            "subject.tar.gz",
                            "sha256:"
                            + hashlib.sha256(b"immutable signed subject bytes").hexdigest(),
                        )
                    )
                ]
            },
            SimpleNamespace(
                returncode=0,
                stdout=_verification_output("other.tar.gz", "sha256:" + "a" * 64),
                stderr="",
            ),
            "ATTESTATION_INVALID",
        ),
    ],
)
def test_unavailable_unknown_or_wrong_subject_proofs_fail_closed(
    tmp_path: Path,
    response: dict[str, object],
    runner_result: SimpleNamespace | None,
    error_code: str,
) -> None:
    payload = b"immutable signed subject bytes"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()

    def runner(_arguments, **_kwargs):
        assert runner_result is not None
        return runner_result

    updater = _updater(tmp_path, response, runner)
    with pytest.raises(updates.UpdateError) as error:
        updater._release_attestation_bundle(
            payload=payload,
            repository="DoHorizon-AI/Cyrene-Platform",
            digest=digest,
            workflow="owner/repo/.github/workflows/release.yml",
            source_ref="refs/heads/develop",
            source_commit="a" * 40,
            subject_name="subject.tar.gz",
        )
    assert error.value.code == error_code


def test_asset_metadata_and_signed_tuple_must_match_downloaded_bytes(tmp_path: Path) -> None:
    payload = b"manifest bytes"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    uri = (
        "https://github.com/DoHorizon-AI/Cyrene-Platform/releases/download/preview-"
        + "a" * 40
        + "/manifest.json"
    )
    assets = (
        {
            "name": "manifest.json",
            "browser_download_url": uri,
            "size": len(payload),
            "digest": digest,
        },
    )
    updater = _updater(tmp_path, {"attestations": []}, lambda *_args, **_kwargs: None)
    updater._get_bytes = lambda _uri, **_kwargs: payload
    assert (
        updater._get_release_asset_bytes(
            assets,
            uri,
            repository="DoHorizon-AI/Cyrene-Platform",
            release_tag="preview-" + "a" * 40,
            expected_digest=digest,
            expected_size=len(payload),
        )
        == payload
    )
    with pytest.raises(updates.UpdateError, match="signed tuple"):
        updater._get_release_asset_bytes(
            assets,
            uri,
            repository="DoHorizon-AI/Cyrene-Platform",
            release_tag="preview-" + "a" * 40,
            expected_digest="sha256:" + "b" * 64,
        )
    updater._get_bytes = lambda _uri, **_kwargs: b"tampered"
    with pytest.raises(updates.UpdateError, match="immutable API size or digest"):
        updater._get_release_asset_bytes(
            assets,
            uri,
            repository="DoHorizon-AI/Cyrene-Platform",
            release_tag="preview-" + "a" * 40,
        )
