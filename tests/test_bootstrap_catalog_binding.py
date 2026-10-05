"""Focused checks for package-pinned bootstrap catalog bindings."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = WORKSPACE_ROOT / "packaging" / "bootstrap_catalog_binding.py"


def _module() -> ModuleType:
    """Load the standalone package helper without relying on a packaging package."""

    spec = importlib.util.spec_from_file_location(
        "cyrene_bootstrap_catalog_binding_test", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_pair(tmp_path: Path, *, generation: int = 12) -> tuple[Path, Path, dict[str, object]]:
    """Write one matching signed catalog and binding pair for a stable release."""

    module = _module()
    catalog_path = tmp_path / "component-catalog-v1.json"
    catalog_bytes = (
        json.dumps({"schemaVersion": 1, "generation": generation}, sort_keys=True) + "\n"
    ).encode()
    catalog_path.write_bytes(catalog_bytes)
    binding_path = tmp_path / "bootstrap-catalog-binding-v1.json"
    catalog_receipt = {
        "repository": module.REPOSITORY,
        "workflow": module.CATALOG_WORKFLOW,
        "releaseId": "catalog-stable-" + "a" * 40,
        "source": {"ref": "refs/heads/main", "commit": "a" * 40},
        "assetName": module.CATALOG_ASSET_NAME,
        "sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "attestationBundleSha256": "b" * 64,
        "generation": generation,
    }
    binding = {"schemaVersion": 1, "catalog": catalog_receipt}
    binding_path.write_text(json.dumps(binding, sort_keys=True) + "\n", encoding="utf-8")
    return binding_path, catalog_path, binding


def test_signed_binding_loads_exact_catalog_bytes_and_generation(tmp_path: Path) -> None:
    module = _module()
    binding_path, catalog_path, binding = _write_pair(tmp_path)

    assert (
        module.load_bootstrap_catalog_binding(binding_path, catalog_path, require_root=False)
        == binding
    )


@pytest.mark.parametrize("missing", ["binding", "catalog"])
def test_loader_rejects_missing_files(tmp_path: Path, missing: str) -> None:
    module = _module()
    binding_path, catalog_path, _ = _write_pair(tmp_path)
    (binding_path if missing == "binding" else catalog_path).unlink()

    with pytest.raises(module.BootstrapCatalogBindingError, match="missing or unsafe"):
        module.load_bootstrap_catalog_binding(binding_path, catalog_path, require_root=False)


@pytest.mark.parametrize("symlink", ["binding", "catalog"])
def test_loader_rejects_symlinked_files(tmp_path: Path, symlink: str) -> None:
    module = _module()
    binding_path, catalog_path, _ = _write_pair(tmp_path)
    selected = binding_path if symlink == "binding" else catalog_path
    original = selected.with_suffix(selected.suffix + ".real")
    selected.rename(original)
    selected.symlink_to(original.name)

    with pytest.raises(module.BootstrapCatalogBindingError, match="regular, non-symlink"):
        module.load_bootstrap_catalog_binding(binding_path, catalog_path, require_root=False)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "DoHorizon-AI/Other"),
        ("workflow", "DoHorizon-AI/Cyrene-Workspace/.github/workflows/other.yml"),
        ("releaseId", "catalog-preview-" + "a" * 40),
        ("sha256", "c" * 64),
    ],
)
def test_loader_rejects_source_identity_and_digest_mismatch(
    tmp_path: Path, field: str, value: str
) -> None:
    module = _module()
    binding_path, catalog_path, binding = _write_pair(tmp_path)
    binding["catalog"][field] = value  # type: ignore[index]
    binding_path.write_text(json.dumps(binding, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(module.BootstrapCatalogBindingError):
        module.load_bootstrap_catalog_binding(binding_path, catalog_path, require_root=False)


def test_loader_rejects_catalog_generation_mismatch(tmp_path: Path) -> None:
    module = _module()
    binding_path, catalog_path, _ = _write_pair(tmp_path)
    catalog_path.write_text('{"schemaVersion":1,"generation":13}\n', encoding="utf-8")

    with pytest.raises(module.BootstrapCatalogBindingError, match="generation"):
        module.load_bootstrap_catalog_binding(binding_path, catalog_path, require_root=False)


def test_development_binding_requires_source_compiled_typed_digest(tmp_path: Path) -> None:
    module = _module()
    catalog_path = tmp_path / module.CATALOG_ASSET_NAME
    catalog_bytes = b'{"schemaVersion":1,"generation":12}\n'
    catalog_path.write_bytes(catalog_bytes)
    digest = hashlib.sha256(catalog_bytes).hexdigest()
    binding_path = tmp_path / module.BINDING_ASSET_NAME
    binding_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "provenance": module.DEVELOPMENT_PROVENANCE,
                "catalog": {
                    "assetName": module.CATALOG_ASSET_NAME,
                    "sha256": digest,
                    "generation": 12,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(module.BootstrapCatalogBindingError, match="source-compiled"):
        module.load_bootstrap_catalog_binding(binding_path, catalog_path, require_root=False)
    assert (
        module.load_bootstrap_catalog_binding(
            binding_path,
            catalog_path,
            require_root=False,
            source_catalog_digest=f"sha256:{digest}",
        )["provenance"]
        == module.DEVELOPMENT_PROVENANCE
    )


def test_root_mode_rejects_noncanonical_paths(tmp_path: Path) -> None:
    module = _module()
    binding_path, catalog_path, _ = _write_pair(tmp_path)

    with pytest.raises(module.BootstrapCatalogBindingError, match="fixed installed paths"):
        module.load_bootstrap_catalog_binding(binding_path, catalog_path)
