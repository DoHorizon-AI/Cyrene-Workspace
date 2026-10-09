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
    *,
    api_calls: list[str] | None = None,
) -> updates.ComponentUpdater:
    subject_digest = "sha256:" + hashlib.sha256(b"immutable signed subject bytes").hexdigest()
    api_uri = "https://api.github.com/repos/DoHorizon-AI/Cyrene-Platform/attestations/"
    api_uri += subject_digest + "?per_page=100"

    def opener(request, timeout):
        assert request.full_url == api_uri
        assert request.get_header("User-agent") == updates.USER_AGENT
        assert timeout == 30
        if api_calls is not None:
            api_calls.append(request.full_url)
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


def test_attestation_cache_reuses_raw_bundle_and_reverifies_each_use(tmp_path: Path) -> None:
    payload = b"immutable signed subject bytes"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    bundle = _bundle_for("subject.tar.gz", digest)
    response = {"attestations": [_bundle_record(bundle)]}
    api_calls: list[str] = []
    verifier_calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        verifier_calls.append(arguments)
        return SimpleNamespace(
            returncode=0, stdout=_verification_output("subject.tar.gz", digest), stderr=""
        )

    first = _updater(tmp_path, response, runner, api_calls=api_calls)
    first_result = first._release_attestation_bundle(
        payload=payload,
        repository="DoHorizon-AI/Cyrene-Platform",
        digest=digest,
        workflow="owner/repo/.github/workflows/release.yml",
        source_ref="refs/heads/develop",
        source_commit="a" * 40,
        subject_name="subject.tar.gz",
    )
    cache_files = list((tmp_path / "state" / "attestations").glob("*.bundle"))
    assert len(cache_files) == 1
    assert cache_files[0].read_bytes() == first_result
    assert stat.S_IMODE(cache_files[0].stat().st_mode) == 0o600

    # A new updater instance models a distinct CLI operation. It must verify the
    # cached bytes again, while avoiding a second REST attestation lookup.
    second = _updater(tmp_path, response, runner, api_calls=api_calls)
    second_result = second._release_attestation_bundle(
        payload=payload,
        repository="DoHorizon-AI/Cyrene-Platform",
        digest=digest,
        workflow="owner/repo/.github/workflows/release.yml",
        source_ref="refs/heads/develop",
        source_commit="a" * 40,
        subject_name="subject.tar.gz",
    )
    assert second_result == first_result
    assert len(api_calls) == 1
    assert len(verifier_calls) == 2


def test_release_sidecar_selects_exact_source_and_cache_reverifies_bundle(
    tmp_path: Path,
) -> None:
    payload = b"immutable signed subject bytes"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    repository = "DoHorizon-AI/Cyrene-Client"
    workflow = f"{repository}/.github/workflows/workspace-web-release.yml"
    old_commit = "297357f08377aee9819d5f14707ec1f8fac3aa88"
    selected_commit = "b3c3f964540e3aa761612371891e3bb6a3f7ad59"
    release_tag = f"preview-cyrene-client-workspace-web-{selected_commit}"
    subject_name = f"cyrene-client-workspace-web-{selected_commit}.tar.gz"
    sidecar_name = f"{subject_name}.attestation.jsonl"
    sidecar_uri = f"https://github.com/{repository}/releases/download/{release_tag}/{sidecar_name}"
    old_bundle = _bundle_for(subject_name, digest)
    old_bundle["verificationMaterial"] = {"fixtureSourceCommit": old_commit}
    selected_bundle = _bundle_for(subject_name, digest)
    selected_bundle["verificationMaterial"] = {"fixtureSourceCommit": selected_commit}
    old_line = json.dumps(old_bundle, separators=(",", ":")).encode()
    selected_line = json.dumps(selected_bundle, separators=(",", ":")).encode()
    sidecar_bytes = old_line + b"\n" + selected_line + b"\n"
    sidecar_digest = "sha256:" + hashlib.sha256(sidecar_bytes).hexdigest()
    release_assets = (
        {
            "name": sidecar_name,
            "browser_download_url": sidecar_uri,
            "size": len(sidecar_bytes),
            "digest": sidecar_digest,
        },
    )
    downloaded: list[str] = []
    api_calls: list[str] = []
    verifier_bundles: list[str] = []

    def runner(arguments, **_kwargs):
        bundle_path = Path(arguments[arguments.index("--bundle") + 1])
        bundle = json.loads(bundle_path.read_bytes())
        signed_source = bundle["verificationMaterial"]["fixtureSourceCommit"]
        verifier_bundles.append(signed_source)
        if signed_source != selected_commit:
            return SimpleNamespace(returncode=1, stdout="", stderr="wrong source commit")
        assert arguments[arguments.index("--repo") + 1] == repository
        assert arguments[arguments.index("--signer-workflow") + 1] == workflow
        assert arguments[arguments.index("--source-ref") + 1] == "refs/heads/develop"
        assert arguments[arguments.index("--source-digest") + 1] == selected_commit
        return SimpleNamespace(
            returncode=0, stdout=_verification_output(subject_name, digest), stderr=""
        )

    def download(uri: str, **_kwargs) -> bytes:
        downloaded.append(uri)
        assert uri == sidecar_uri
        return sidecar_bytes

    first = _updater(tmp_path, {"attestations": []}, runner, api_calls=api_calls)
    first._get_bytes = download
    first_result = first._release_attestation_bundle(
        payload=payload,
        repository=repository,
        digest=digest,
        workflow=workflow,
        source_ref="refs/heads/develop",
        source_commit=selected_commit,
        subject_name=subject_name,
        release_assets=release_assets,
        release_tag=release_tag,
    )
    assert first_result == selected_line
    cached_bundles = list((tmp_path / "state" / "attestations").glob("*.bundle"))
    assert len(cached_bundles) == 1
    assert cached_bundles[0].read_bytes() == selected_line
    assert verifier_bundles == [old_commit, selected_commit]

    second = _updater(tmp_path, {"attestations": []}, runner, api_calls=api_calls)
    second._get_bytes = lambda *_args, **_kwargs: pytest.fail(
        "verified sidecar cache should avoid another download"
    )
    second_result = second._release_attestation_bundle(
        payload=payload,
        repository=repository,
        digest=digest,
        workflow=workflow,
        source_ref="refs/heads/develop",
        source_commit=selected_commit,
        subject_name=subject_name,
        release_assets=release_assets,
        release_tag=release_tag,
    )
    assert second_result == selected_line
    assert downloaded == [sidecar_uri]
    assert api_calls == []
    assert verifier_bundles == [old_commit, selected_commit, selected_commit]


@pytest.mark.parametrize(
    "failure", ["malformed-line", "wrong-size", "wrong-digest", "no-subject", "wrong-context"]
)
def test_present_invalid_release_sidecar_fails_closed_without_api_fallback(
    tmp_path: Path, failure: str
) -> None:
    payload = b"immutable signed subject bytes"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    release_tag = "preview-component-" + "c" * 40
    sidecar_name = "subject.tar.gz.attestation.jsonl"
    sidecar_uri = (
        "https://github.com/DoHorizon-AI/Cyrene-Platform/releases/download/"
        f"{release_tag}/{sidecar_name}"
    )
    subject_name = "other.tar.gz" if failure == "no-subject" else "subject.tar.gz"
    bundle = _bundle_for(subject_name, digest)
    if failure == "wrong-context":
        bundle["verificationMaterial"] = {"fixtureSourceCommit": "a" * 40}
    line = json.dumps(bundle, separators=(",", ":")).encode()
    sidecar_bytes = line + (b"\nnot-json\n" if failure == "malformed-line" else b"\n")
    metadata_digest = "sha256:" + hashlib.sha256(sidecar_bytes).hexdigest()
    metadata_size = len(sidecar_bytes)
    if failure == "wrong-size":
        metadata_size += 1
    elif failure == "wrong-digest":
        metadata_digest = "sha256:" + "0" * 64
    release_assets = (
        {
            "name": sidecar_name,
            "browser_download_url": sidecar_uri,
            "size": metadata_size,
            "digest": metadata_digest,
        },
    )
    api_calls: list[str] = []
    downloads: list[str] = []

    def download(uri: str, **_kwargs) -> bytes:
        downloads.append(uri)
        assert uri == sidecar_uri
        return sidecar_bytes

    updater = _updater(
        tmp_path,
        {"attestations": [_bundle_record(_bundle_for("subject.tar.gz", digest))]},
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1 if failure == "wrong-context" else 0,
            stdout=""
            if failure == "wrong-context"
            else _verification_output("subject.tar.gz", digest),
            stderr="wrong source context" if failure == "wrong-context" else "",
        ),
        api_calls=api_calls,
    )
    updater._get_bytes = download
    with pytest.raises(updates.UpdateError) as error:
        updater._release_attestation_bundle(
            payload=payload,
            repository="DoHorizon-AI/Cyrene-Platform",
            digest=digest,
            workflow="owner/repo/.github/workflows/release.yml",
            source_ref="refs/heads/develop",
            source_commit="b" * 40,
            subject_name="subject.tar.gz",
            release_assets=release_assets,
            release_tag=release_tag,
        )

    assert error.value.code == (
        "INVALID_ATTESTATION"
        if failure == "malformed-line"
        else "ATTESTATION_INVALID"
        if failure in {"no-subject", "wrong-context"}
        else "RELEASE_ASSET_DIGEST_MISMATCH"
    )
    assert downloads == [sidecar_uri]
    assert api_calls == []
    assert not list((tmp_path / "state" / "attestations").glob("*.bundle"))


def test_missing_release_sidecar_uses_existing_attestation_api(tmp_path: Path) -> None:
    payload = b"immutable signed subject bytes"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    bundle = _bundle_for("subject.tar.gz", digest)
    api_calls: list[str] = []
    updater = _updater(
        tmp_path,
        {"attestations": [_bundle_record(bundle)]},
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=_verification_output("subject.tar.gz", digest), stderr=""
        ),
        api_calls=api_calls,
    )
    updater._release_attestation_bundle(
        payload=payload,
        repository="DoHorizon-AI/Cyrene-Platform",
        digest=digest,
        workflow="owner/repo/.github/workflows/release.yml",
        source_ref="refs/heads/develop",
        source_commit="a" * 40,
        subject_name="subject.tar.gz",
        release_assets=(),
        release_tag="preview-component-" + "c" * 40,
    )
    assert len(api_calls) == 1


def test_attestation_cache_key_separates_source_context_and_does_not_cache_failure(
    tmp_path: Path,
) -> None:
    payload = b"immutable signed subject bytes"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    bundle = _bundle_for("subject.tar.gz", digest)
    api_calls: list[str] = []
    verifier_contexts: list[tuple[str, str, str]] = []

    def runner(arguments, **_kwargs):
        workflow = arguments[arguments.index("--signer-workflow") + 1]
        source_ref = arguments[arguments.index("--source-ref") + 1]
        source_commit = arguments[arguments.index("--source-digest") + 1]
        verifier_contexts.append((workflow, source_ref, source_commit))
        if workflow.endswith("other.yml"):
            return SimpleNamespace(returncode=1, stdout="", stderr="wrong signed context")
        return SimpleNamespace(
            returncode=0, stdout=_verification_output("subject.tar.gz", digest), stderr=""
        )

    updater = _updater(
        tmp_path,
        {"attestations": [_bundle_record(bundle)]},
        runner,
        api_calls=api_calls,
    )
    updater._release_attestation_bundle(
        payload=payload,
        repository="DoHorizon-AI/Cyrene-Platform",
        digest=digest,
        workflow="owner/repo/.github/workflows/release.yml",
        source_ref="refs/heads/develop",
        source_commit="a" * 40,
        subject_name="subject.tar.gz",
    )
    with pytest.raises(updates.UpdateError) as error:
        updater._release_attestation_bundle(
            payload=payload,
            repository="DoHorizon-AI/Cyrene-Platform",
            digest=digest,
            workflow="owner/repo/.github/workflows/other.yml",
            source_ref="refs/heads/main",
            source_commit="b" * 40,
            subject_name="subject.tar.gz",
        )

    assert error.value.code == "ATTESTATION_INVALID"
    assert len(api_calls) == 2
    assert verifier_contexts == [
        ("owner/repo/.github/workflows/release.yml", "refs/heads/develop", "a" * 40),
        ("owner/repo/.github/workflows/other.yml", "refs/heads/main", "b" * 40),
    ]
    assert len(list((tmp_path / "state" / "attestations").glob("*.bundle"))) == 1


def test_tampered_attestation_cache_fails_closed_without_network_fallback(tmp_path: Path) -> None:
    payload = b"immutable signed subject bytes"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    bundle = _bundle_for("subject.tar.gz", digest)
    response = {"attestations": [_bundle_record(bundle)]}
    api_calls: list[str] = []
    runner_calls: list[list[str]] = []

    def runner(arguments, **_kwargs):
        runner_calls.append(arguments)
        return SimpleNamespace(
            returncode=0, stdout=_verification_output("subject.tar.gz", digest), stderr=""
        )

    first = _updater(tmp_path, response, runner, api_calls=api_calls)
    first._release_attestation_bundle(
        payload=payload,
        repository="DoHorizon-AI/Cyrene-Platform",
        digest=digest,
        workflow="owner/repo/.github/workflows/release.yml",
        source_ref="refs/heads/develop",
        source_commit="a" * 40,
        subject_name="subject.tar.gz",
    )
    cache_file = next((tmp_path / "state" / "attestations").glob("*.bundle"))
    cache_file.write_bytes(
        json.dumps(_bundle_for("other.tar.gz", digest), separators=(",", ":")).encode()
    )

    second = _updater(tmp_path, response, runner, api_calls=api_calls)
    with pytest.raises(updates.UpdateError) as error:
        second._release_attestation_bundle(
            payload=payload,
            repository="DoHorizon-AI/Cyrene-Platform",
            digest=digest,
            workflow="owner/repo/.github/workflows/release.yml",
            source_ref="refs/heads/develop",
            source_commit="a" * 40,
            subject_name="subject.tar.gz",
        )

    assert error.value.code == "ATTESTATION_SUBJECT_MISMATCH"
    assert len(api_calls) == 1
    assert len(runner_calls) == 1

    cache_file.chmod(0o644)
    third = _updater(tmp_path, response, runner, api_calls=api_calls)
    with pytest.raises(updates.UpdateError) as unsafe_metadata:
        third._release_attestation_bundle(
            payload=payload,
            repository="DoHorizon-AI/Cyrene-Platform",
            digest=digest,
            workflow="owner/repo/.github/workflows/release.yml",
            source_ref="refs/heads/develop",
            source_commit="a" * 40,
            subject_name="subject.tar.gz",
        )
    assert unsafe_metadata.value.code == "UNSAFE_STATE"
    assert len(api_calls) == 1


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
    assert not list((tmp_path / "state" / "attestations").glob("*.bundle"))


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
