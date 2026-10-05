"""Trust-boundary tests for Product Package Runtime environment projection."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = _load_module(
    "native_package_runtime_bootstrap_environment_test",
    ROOT / "packaging/native_package_runtime_bootstrap.py",
)
environment = _load_module(
    "native_product_package_environment_test",
    ROOT / "packaging/native_product_package_environment.py",
)


@pytest.fixture
def projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Build a valid admitted receipt, current catalog, and protected token files."""

    monkeypatch.setattr(environment.os, "geteuid", lambda: 0)
    monkeypatch.setattr(environment.os, "fchown", lambda _descriptor, _uid, _gid: None)
    monkeypatch.setattr(bootstrap, "_runtime_user_id", lambda: 1201)
    monkeypatch.setattr(bootstrap, "_runtime_group_id", lambda: 1201)

    def require_directory(path: Path, *, mode: int, create: bool) -> None:
        if not path.exists():
            if not create:
                raise environment.ProductPackageEnvironmentError("directory unavailable")
            path.mkdir(mode=mode)
        info = path.lstat()
        if path.is_symlink() or not info.st_mode & 0o40000 or info.st_mode & 0o777 != mode:
            raise environment.ProductPackageEnvironmentError("directory metadata unsafe")

    def read_regular_file(path: Path, *, mode: int, maximum: int) -> bytes:
        info = path.lstat()
        if (
            path.is_symlink()
            or not info.st_mode & 0o100000
            or info.st_mode & 0o777 != mode
            or info.st_nlink != 1
            or info.st_size < 1
            or info.st_size > maximum
        ):
            raise environment.ProductPackageEnvironmentError("credential metadata unsafe")
        return path.read_bytes()

    monkeypatch.setattr(environment, "_require_directory", require_directory)
    monkeypatch.setattr(environment, "_read_regular_file", read_regular_file)
    candidate = bootstrap.VerifiedPackageCandidate(
        package_id=bootstrap.PACKAGE_ID,
        package_version=bootstrap.PACKAGE_VERSION,
        capability=bootstrap.PACKAGE_CAPABILITY,
        interface_version=bootstrap.PACKAGE_INTERFACE_VERSION,
        source_ref=bootstrap.PACKAGE_SOURCE_REF,
        source_commit=bootstrap.PACKAGE_SOURCE_COMMIT,
        release_digest="sha256:" + "1" * 64,
        descriptor_digest="sha256:" + "2" * 64,
        manifest_digest="sha256:" + "3" * 64,
        artifact_digest="sha256:" + "4" * 64,
        archive_digest="sha256:" + "5" * 64,
        dependency_lock_digest="sha256:" + "6" * 64,
        preparer_wheel_digest="sha256:" + "7" * 64,
        attestation_bundle_digests={},
        release_path=tmp_path / "release.json",
        descriptor_path=tmp_path / "descriptor.json",
        archive_path=tmp_path / "archive.zip",
        dependency_lock_path=tmp_path / "requirements.lock",
        preparer_wheel_path=tmp_path / "preparer.whl",
    )
    installation_id = bootstrap._expected_installation_id(candidate)
    receipt = {
        "record_version": 1,
        "installation_id": installation_id,
        "package_id": candidate.package_id,
        "package_version": candidate.package_version,
        "artifact_digest": candidate.artifact_digest,
        "archive_digest": candidate.archive_digest,
        "capabilities": [candidate.capability],
        "state": "INSTALLED",
        "verification": {
            "verifier": "cy-package-runtime",
            "verified_at_unix_ms": 100,
            "artifact_digest": candidate.artifact_digest,
            "archive_digest": candidate.archive_digest,
            "descriptor_digest": candidate.descriptor_digest,
            "manifest_digest": candidate.manifest_digest,
            "dependency_lock_digest": candidate.dependency_lock_digest,
        },
        "dependencies": {
            "preparer": "cyrene-plugin-python-preparer",
            "prepared_at_unix_ms": 100,
            "lock_digest": candidate.dependency_lock_digest,
            "runtime_digest": "sha256:" + "8" * 64,
            "runtime_executable": "/var/lib/cyrene/package-runtime/python",
        },
        "installed_at_unix_ms": 100,
    }
    catalog = {
        "schema_version": 1,
        "generation": 42,
        "sources": [
            {
                "source_id": bootstrap.PACKAGE_SOURCE_ID,
                "uid": 1201,
                "gid": 1201,
                "source_token_sha256": hashlib.sha256(b"existing source token").hexdigest(),
                "binding_scopes": bootstrap.build_binding_scope_input(candidate, receipt)[
                    bootstrap.PACKAGE_SOURCE_ID
                ],
            }
        ],
    }
    policy = bootstrap.build_runtime_source_policy(candidate, receipt, catalog)
    source_token_file = tmp_path / "cyrene-yield.token"
    source_token_file.write_bytes(b"existing source token\n")
    source_token_file.chmod(0o400)
    monkeypatch.setattr(
        bootstrap,
        "_read_source_token",
        lambda path, digest: (
            path.read_text(encoding="ascii").strip()
            if hashlib.sha256(path.read_text(encoding="ascii").strip().encode()).hexdigest()
            == digest
            else pytest.fail("existing activity token did not match the catalog digest")
        ),
    )
    env_dir = tmp_path / "etc-cyrene"
    env_dir.mkdir(mode=0o755)
    env_dir.chmod(0o755)
    activity_environment_file = env_dir / "runtime-activity-sources.env"
    activity_environment_file.write_text(
        "CYRENE_RUNTIME_ACTIVITY_CATALOG_GENERATION=41\n", encoding="ascii"
    )
    activity_environment_file.chmod(0o644)
    return {
        "candidate": candidate,
        "receipt": receipt,
        "catalog": catalog,
        "policy": policy,
        "source_token": source_token_file,
        "environment": env_dir / "yield-package-runtime.env",
        "env": env_dir / "yield-package-runtime.env",
        "activity_environment": activity_environment_file,
        "owner_dir": tmp_path / "owner-tokens",
        "owner_token": tmp_path / "owner-tokens" / "yield.token",
    }


def _generate(values: dict[str, Any]) -> Any:
    return environment.ensure_yield_package_environment(
        bootstrap,
        values["candidate"],
        values["receipt"],
        values["catalog"],
        values["policy"],
        previous_catalog_generation=41,
        activity_token_file=values["source_token"],
        activity_environment_path=values["activity_environment"],
        environment_path=values["env"],
        owner_token_directory=values["owner_dir"],
        owner_token_file=values["owner_token"],
    )


def test_projects_exact_sdk_and_owner_environment_without_replacing_source_token(
    projection: dict[str, Any],
) -> None:
    """Use the current admitted generation while keeping source and owner auth separate."""

    before = projection["source_token"].read_bytes()
    result = _generate(projection)
    owner_token = projection["owner_token"].read_text(encoding="ascii").strip()
    expected = {
        "CYRENE_RUNTIME_ACTIVITY_SOURCE_ID": "cyrene-yield",
        "CYRENE_RUNTIME_ACTIVITY_SOURCE_TOKEN_FILE": "%d/activity-token",
        "CYRENE_RUNTIME_ACTIVITY_CATALOG_GENERATION": "42",
        "CYRENE_RUNTIME_MAINTENANCE_SOCKET": "/run/cyrene/runtime-maintenance.sock",
        "CYRENE_PACKAGE_RUNTIME_SOCKET": str(bootstrap.PACKAGE_RUNTIME_CONTROL_SOCKET),
        "CYRENE_LLAMA_FACTORY_PACKAGE_BINDING_ID": bootstrap.PACKAGE_BINDING_ID,
        "YIELD_RUNTIME_OWNER_TOKEN_SHA256": hashlib.sha256(owner_token.encode()).hexdigest(),
    }
    generated = dict(
        line.split("=", 1) for line in projection["env"].read_text(encoding="ascii").splitlines()
    )
    shared = dict(
        line.split("=", 1)
        for line in projection["activity_environment"].read_text(encoding="ascii").splitlines()
    )
    actual = {
        **shared,
        "CYRENE_RUNTIME_ACTIVITY_SOURCE_ID": "cyrene-yield",
        "CYRENE_RUNTIME_ACTIVITY_SOURCE_TOKEN_FILE": "%d/activity-token",
        "CYRENE_RUNTIME_MAINTENANCE_SOCKET": "/run/cyrene/runtime-maintenance.sock",
        **generated,
    }

    assert actual == expected
    assert projection["activity_environment"].read_text(encoding="ascii") == (
        "CYRENE_RUNTIME_ACTIVITY_CATALOG_GENERATION=42\n"
    )
    assert result.owner_token_path == projection["owner_token"]
    assert result.changed is True
    assert projection["source_token"].read_bytes() == before
    assert owner_token.encode() not in projection["env"].read_bytes()
    assert owner_token not in repr(result)
    assert projection["owner_token"].stat().st_mode & 0o777 == 0o600
    assert projection["env"].stat().st_mode & 0o777 == 0o644


def test_repeated_projection_reuses_the_same_owner_bearer_and_files(
    projection: dict[str, Any],
) -> None:
    """Retries preserve stable credentials and avoid needless environment rewrites."""

    _generate(projection)
    token = projection["owner_token"].read_bytes()
    env_stat = projection["env"].stat()
    result = _generate(projection)

    assert projection["owner_token"].read_bytes() == token
    assert projection["env"].stat().st_ino == env_stat.st_ino
    assert result.changed is False


def test_rejects_stale_generation_or_binding_scope(projection: dict[str, Any]) -> None:
    """The env projection follows the validated install receipt and catalog policy."""

    projection["policy"] = {**projection["policy"], "generation": 41}
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError):
        _generate(projection)
    assert not projection["env"].exists()


def test_rejects_activity_environment_from_an_unrelated_generation(
    projection: dict[str, Any],
) -> None:
    """Only the exact pre-update or already-updated catalog generation is accepted."""

    projection["activity_environment"].write_text(
        "CYRENE_RUNTIME_ACTIVITY_CATALOG_GENERATION=39\n", encoding="ascii"
    )
    with pytest.raises(environment.ProductPackageEnvironmentError):
        _generate(projection)


@pytest.mark.parametrize("unsafe_target", ["environment", "owner_token"])
def test_rejects_symlink_targets(projection: dict[str, Any], unsafe_target: str) -> None:
    """Neither generated output path may be redirected through a link."""

    target = projection[unsafe_target]
    if unsafe_target == "owner_token":
        projection["owner_dir"].mkdir(mode=0o700)
    elsewhere = target.parent / "elsewhere"
    elsewhere.write_text("unexpected", encoding="ascii")
    target.symlink_to(elsewhere)

    with pytest.raises(environment.ProductPackageEnvironmentError):
        _generate(projection)


def test_rejects_owner_token_with_unsafe_mode(projection: dict[str, Any]) -> None:
    """An existing bearer with broader permissions is never silently accepted."""

    projection["owner_dir"].mkdir(mode=0o700)
    projection["owner_token"].write_text("fixture bearer\n", encoding="ascii")
    projection["owner_token"].chmod(0o640)

    with pytest.raises(environment.ProductPackageEnvironmentError):
        _generate(projection)


def test_rejects_untrusted_yield_scope(projection: dict[str, Any]) -> None:
    """Additional operations or installation IDs cannot widen admitted authority."""

    projection["catalog"]["sources"][0]["binding_scopes"][0]["operations"].append(
        "another-operation"
    )
    with pytest.raises(bootstrap.PackageRuntimeBootstrapError):
        _generate(projection)
