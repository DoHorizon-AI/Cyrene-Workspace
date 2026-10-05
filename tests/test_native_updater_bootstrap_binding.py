"""Focused tests for the installed updater's immutable bootstrap catalog binding."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPDATES_PATH = ROOT / "packaging" / "component_updates.py"
_UPDATES_SPEC = importlib.util.spec_from_file_location(
    "native_updater_binding_component_updates", UPDATES_PATH
)
assert _UPDATES_SPEC is not None and _UPDATES_SPEC.loader is not None
updates = importlib.util.module_from_spec(_UPDATES_SPEC)
sys.modules[_UPDATES_SPEC.name] = updates
_UPDATES_SPEC.loader.exec_module(updates)

BINDING_PATH = ROOT / "packaging" / "bootstrap_catalog_binding.py"
_BINDING_SPEC = importlib.util.spec_from_file_location(
    "native_updater_binding_schema", BINDING_PATH
)
assert _BINDING_SPEC is not None and _BINDING_SPEC.loader is not None
binding_schema = importlib.util.module_from_spec(_BINDING_SPEC)
sys.modules[_BINDING_SPEC.name] = binding_schema
_BINDING_SPEC.loader.exec_module(binding_schema)


def _catalog_bytes(*, generation: int | None = None) -> bytes:
    document = json.loads(
        (ROOT / "packaging" / "component-catalog-bootstrap-v1.json").read_text(encoding="utf-8")
    )
    if generation is not None:
        document["generation"] = generation
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o644)
    return path


def _signed_binding(catalog: bytes) -> dict[str, Any]:
    document = json.loads(catalog)
    commit = "a" * 40
    return {
        "schemaVersion": 1,
        "catalog": {
            "repository": binding_schema.REPOSITORY,
            "workflow": binding_schema.CATALOG_WORKFLOW,
            "releaseId": f"catalog-stable-{commit}",
            "source": {"ref": "refs/heads/main", "commit": commit},
            "assetName": binding_schema.CATALOG_ASSET_NAME,
            "sha256": hashlib.sha256(catalog).hexdigest(),
            "attestationBundleSha256": "b" * 64,
            "generation": document["generation"],
        },
    }


def _write_binding(path: Path, document: dict[str, Any]) -> Path:
    return _write(path, json.dumps(document, sort_keys=True, indent=2).encode() + b"\n")


@pytest.fixture(autouse=True)
def _simulate_root_owned_test_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original_lstat = Path.lstat
    test_root = tmp_path.resolve()

    def root_owned_lstat(path: Path) -> os.stat_result:
        metadata = original_lstat(path)
        if not path.absolute().is_relative_to(test_root):
            return metadata
        fields = list(metadata)
        fields[4] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", root_owned_lstat)


def _install_runtime_binding_context(
    monkeypatch: pytest.MonkeyPatch,
    catalog_path: Path,
    binding_path: Path,
) -> list[tuple[Path, Path, str | None]]:
    """Run the installed branch while using the real loader's byte validation."""

    calls: list[tuple[Path, Path, str | None]] = []
    monkeypatch.setattr(updates, "INSTALLED_HELPER_DIRECTORY", UPDATES_PATH.parent)
    monkeypatch.setattr(updates, "INSTALLED_CATALOG", catalog_path)
    monkeypatch.setattr(updates, "INSTALLED_BOOTSTRAP_CATALOG_BINDING", binding_path)
    original_loader = binding_schema.load_bootstrap_catalog_binding

    def load_for_test(self: Any) -> dict[str, Any]:
        calls.append(
            (
                updates.INSTALLED_BOOTSTRAP_CATALOG_BINDING,
                updates.INSTALLED_CATALOG,
                updates.TRUSTED_CATALOG_DIGEST,
            )
        )
        try:
            return original_loader(
                updates.INSTALLED_BOOTSTRAP_CATALOG_BINDING,
                updates.INSTALLED_CATALOG,
                require_root=False,
                source_catalog_digest=updates.TRUSTED_CATALOG_DIGEST,
            )
        except Exception as error:
            raise updates.UpdateError("INVALID_BOOTSTRAP_BINDING", str(error)) from error

    monkeypatch.setattr(
        updates.ComponentUpdater, "_load_installed_bootstrap_catalog_binding", load_for_test
    )
    return calls


def _updater(
    catalog_path: Path,
    *,
    trusted_catalog_digest: str | None = updates.TRUSTED_CATALOG_DIGEST,
    load_active_catalog: bool = False,
) -> Any:
    return updates.ComponentUpdater(
        catalog_path=catalog_path,
        activity_catalog_path=catalog_path.parent / "activity-sources.json",
        state_root=catalog_path.parent / "updater-state",
        install_root=catalog_path.parent / "install-root",
        release_lock_path=catalog_path.parent / "missing-release-lock.json",
        trusted_catalog_digest=trusted_catalog_digest,
        load_active_catalog=load_active_catalog,
    )


def test_installed_signed_binding_selects_catalog_and_ignores_caller_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path = _write(tmp_path / "usr/share/cyrene/component-catalog-v1.json", _catalog_bytes())
    binding_path = _write_binding(
        tmp_path / "usr/share/cyrene/bootstrap-catalog-binding-v1.json",
        _signed_binding(catalog_path.read_bytes()),
    )
    calls = _install_runtime_binding_context(monkeypatch, catalog_path, binding_path)
    assert catalog_path.lstat().st_uid == 0
    assert stat.S_IMODE(catalog_path.lstat().st_mode) & 0o022 == 0

    arbitrary_caller_digest = "sha256:" + "c" * 64
    updater = _updater(catalog_path, trusted_catalog_digest=arbitrary_caller_digest)

    assert updater.bootstrap_catalog_binding == _signed_binding(catalog_path.read_bytes())
    assert (
        updater.bootstrap_catalog_digest
        == "sha256:" + hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    )
    assert updater.bootstrap_catalog_authorized is True
    assert calls == [(binding_path, catalog_path, updates.TRUSTED_CATALOG_DIGEST)]


def test_installed_dev_binding_uses_only_source_compiled_pin_even_when_digest_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = (ROOT / "packaging" / "component-catalog-bootstrap-v1.json").read_bytes()
    catalog_path = _write(tmp_path / "usr/share/cyrene/component-catalog-v1.json", catalog)
    binding_path = _write_binding(
        tmp_path / "usr/share/cyrene/bootstrap-catalog-binding-v1.json",
        {
            "schemaVersion": 1,
            "provenance": binding_schema.DEVELOPMENT_PROVENANCE,
            "catalog": {
                "assetName": binding_schema.CATALOG_ASSET_NAME,
                "sha256": hashlib.sha256(catalog).hexdigest(),
                "generation": json.loads(catalog)["generation"],
            },
        },
    )
    calls = _install_runtime_binding_context(monkeypatch, catalog_path, binding_path)

    updater = _updater(catalog_path, trusted_catalog_digest=None)

    assert updater.bootstrap_catalog_authorized is True
    assert calls[0][2] == updates.TRUSTED_CATALOG_DIGEST


@pytest.mark.parametrize(
    "mutate",
    [
        lambda binding: binding["catalog"].update(sha256="d" * 64),
        lambda binding: binding["catalog"].update(unexpected=True),
        lambda binding: binding.update(provenance="development-source-pin"),
    ],
)
def test_installed_malformed_or_substituted_binding_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate
) -> None:
    catalog_path = _write(tmp_path / "usr/share/cyrene/component-catalog-v1.json", _catalog_bytes())
    binding = _signed_binding(catalog_path.read_bytes())
    mutate(binding)
    binding_path = _write_binding(
        tmp_path / "usr/share/cyrene/bootstrap-catalog-binding-v1.json", binding
    )
    _install_runtime_binding_context(monkeypatch, catalog_path, binding_path)

    with pytest.raises(updates.UpdateError) as error:
        _updater(catalog_path)

    assert error.value.code == "INVALID_BOOTSTRAP_BINDING"


def test_installed_helper_rejects_caller_catalog_path_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed_catalog = _write(
        tmp_path / "usr/share/cyrene/component-catalog-v1.json", _catalog_bytes()
    )
    installed_binding = _write_binding(
        tmp_path / "usr/share/cyrene/bootstrap-catalog-binding-v1.json",
        _signed_binding(installed_catalog.read_bytes()),
    )
    _install_runtime_binding_context(monkeypatch, installed_catalog, installed_binding)
    arbitrary_catalog = _write(tmp_path / "caller/catalog.json", _catalog_bytes(generation=13))

    with pytest.raises(updates.UpdateError, match="fixed bootstrap catalog path"):
        _updater(arbitrary_catalog)


def test_active_catalog_remains_selected_above_bound_bootstrap_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path = _write(tmp_path / "usr/share/cyrene/component-catalog-v1.json", _catalog_bytes())
    binding_path = _write_binding(
        tmp_path / "usr/share/cyrene/bootstrap-catalog-binding-v1.json",
        _signed_binding(catalog_path.read_bytes()),
    )
    _install_runtime_binding_context(monkeypatch, catalog_path, binding_path)
    active_bytes = _catalog_bytes(generation=13)
    active_digest = "sha256:" + hashlib.sha256(active_bytes).hexdigest()
    monkeypatch.setattr(
        updates.ComponentUpdater,
        "_read_active_catalog",
        lambda _self: (active_bytes, {"catalogSha256": active_digest, "generation": 13}),
    )

    updater = _updater(catalog_path, load_active_catalog=True)

    assert updater.catalog_generation == 13
    assert updater.catalog_digest == active_digest
    assert updater.bootstrap_catalog_digest == (
        "sha256:" + updater.bootstrap_catalog_binding["catalog"]["sha256"]
    )


def test_source_custom_none_digest_stays_unpinned_and_cannot_authorize_other_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates, "INSTALLED_HELPER_DIRECTORY", Path("/usr/lib/cyrene/scripts"))
    custom_catalog = _write(tmp_path / "custom/catalog.json", _catalog_bytes(generation=13))

    updater = _updater(custom_catalog, trusted_catalog_digest=None)

    assert updater.bootstrap_catalog_authorized is False
    with pytest.raises(updates.UpdateError) as error:
        updater._resolve_installed_broker()
    assert error.value.code == "GATE_UNKNOWN"


def test_source_custom_explicit_pin_cannot_authorize_a_noncompiled_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates, "INSTALLED_HELPER_DIRECTORY", Path("/usr/lib/cyrene/scripts"))
    custom_catalog = _write(tmp_path / "custom/catalog.json", _catalog_bytes(generation=13))
    caller_pin = "sha256:" + hashlib.sha256(custom_catalog.read_bytes()).hexdigest()

    updater = _updater(custom_catalog, trusted_catalog_digest=caller_pin)

    assert updater.bootstrap_catalog_digest == caller_pin
    assert updater.bootstrap_catalog_authorized is False
    with pytest.raises(updates.UpdateError) as error:
        updater._resolve_installed_broker()
    assert error.value.code == "GATE_UNKNOWN"


def test_source_none_digest_preserves_the_exact_compiled_catalog_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates, "INSTALLED_HELPER_DIRECTORY", Path("/usr/lib/cyrene/scripts"))
    source_catalog = ROOT / "packaging" / "component-catalog-bootstrap-v1.json"

    updater = _updater(source_catalog, trusted_catalog_digest=None)

    assert updater.bootstrap_catalog_digest == updates.TRUSTED_CATALOG_DIGEST
    assert updater.bootstrap_catalog_authorized is True
