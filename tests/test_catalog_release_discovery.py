"""Verify version-separated catalog release selection and exact asset binding."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _metadata_module():
    name = "cyrene_catalog_release_discovery_test"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = ROOT / "packaging" / "catalog_metadata.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _release(tag: str, created_at: str) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "immutable": True,
        "draft": False,
        "prerelease": "preview" in tag,
        "created_at": created_at,
    }


def test_latest_release_prefers_v2_before_comparing_creation_time(monkeypatch) -> None:
    metadata = _metadata_module()
    releases = [
        _release("catalog-preview-" + "a" * 40, "2026-10-08T12:00:00Z"),
        _release("catalog-v2-preview-" + "b" * 40, "2026-10-07T12:00:00Z"),
    ]
    monkeypatch.setattr(metadata, "_read_api_json", lambda _url: releases)

    selected = metadata._latest_immutable_release("preview")

    assert selected["tag_name"] == releases[1]["tag_name"]


def test_latest_release_falls_back_to_v1_until_a_v2_release_exists(monkeypatch) -> None:
    metadata = _metadata_module()
    releases = [
        _release("catalog-preview-" + "a" * 40, "2026-10-07T12:00:00Z"),
        _release("catalog-preview-" + "b" * 40, "2026-10-08T12:00:00Z"),
    ]
    monkeypatch.setattr(metadata, "_read_api_json", lambda _url: releases)

    assert metadata._latest_immutable_release("preview")["tag_name"] == releases[1]["tag_name"]


@pytest.mark.parametrize(
    ("tag", "version"),
    [
        ("catalog-preview-" + "c" * 40, 1),
        ("catalog-v2-preview-" + "c" * 40, 2),
    ],
)
def test_release_tag_namespace_selects_only_its_catalog_asset_pair(tag: str, version: int) -> None:
    metadata = _metadata_module()
    catalog_name, attestation_name = metadata.CATALOG_ASSETS[version]
    assets = [
        {"name": catalog_name, "id": 1, "size": 1, "state": "uploaded"},
        {"name": attestation_name, "id": 2, "size": 1, "state": "uploaded"},
    ]
    for asset in assets:
        asset["url"] = (
            f"https://api.github.com/repos/{metadata.REPOSITORY}/releases/assets/{asset['id']}"
        )
        asset["browser_download_url"] = metadata._expected_browser_url(tag, asset["name"])

    selected = metadata._find_assets({"assets": assets}, tag)

    assert selected[1] == catalog_name
    assert selected[2] == attestation_name


def test_release_tag_rejects_catalog_asset_version_mismatch() -> None:
    metadata = _metadata_module()
    tag = "catalog-v2-preview-" + "d" * 40
    catalog_name, attestation_name = metadata.CATALOG_ASSETS[1]
    assets = []
    for asset_id, name in enumerate((catalog_name, attestation_name), start=1):
        assets.append(
            {
                "name": name,
                "id": asset_id,
                "size": 1,
                "state": "uploaded",
                "url": f"https://api.github.com/repos/{metadata.REPOSITORY}/releases/assets/{asset_id}",
                "browser_download_url": metadata._expected_browser_url(tag, name),
            }
        )

    with pytest.raises(metadata.CatalogMetadataError, match="schema version"):
        metadata._find_assets({"assets": assets}, tag)


def test_v2_tag_cannot_carry_a_v1_payload(monkeypatch, tmp_path: Path) -> None:
    metadata = _metadata_module()
    tag = "catalog-v2-preview-" + "e" * 40
    release = _release(tag, "2026-10-08T12:00:00Z")
    release["prerelease"] = True
    monkeypatch.setattr(metadata, "_release_by_tag", lambda _tag: release)
    monkeypatch.setattr(
        metadata,
        "_find_assets",
        lambda _release, _tag: (
            {"component-catalog-v2.json": ("raw", 3), "proof": ("proof", 5)},
            "component-catalog-v2.json",
            "proof",
        ),
    )
    monkeypatch.setattr(metadata, "_download_asset", lambda _url, **_kwargs: b"raw")
    monkeypatch.setattr(metadata, "_validate_catalog", lambda _payload, _root: {"schemaVersion": 1})

    with pytest.raises(metadata.CatalogMetadataError, match="tag schema version"):
        metadata.fetch_verified_catalog(
            channel="preview",
            release_id=tag,
            output_path=tmp_path / "catalog.json",
            metadata_path=tmp_path / "metadata.json",
            schema_root=ROOT / "governance",
            verify_attestation=lambda *_args, **_kwargs: True,
        )
