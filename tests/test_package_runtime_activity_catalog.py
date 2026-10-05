"""Strict optional Package Runtime scopes in the updater activity catalog."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

UPDATES_PATH = Path(__file__).resolve().parents[1] / "packaging/component_updates.py"
SPEC = importlib.util.spec_from_file_location("package_runtime_activity_catalog_test", UPDATES_PATH)
assert SPEC is not None and SPEC.loader is not None
updates = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = updates
SPEC.loader.exec_module(updates)

PACKAGE_SOURCE = {
    "source_id": "cyrene-yield",
    "uid": 1000,
    "gid": 998,
    "source_token_sha256": "a" * 64,
}


def _scope(
    *,
    binding_id: str = "yield.llama-factory.primary",
    package_id: str = "cyrene.training.llama-factory",
    installation_ids: list[str] | None = None,
    operations: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "binding_id": binding_id,
        "package_id": package_id,
        "installation_ids": installation_ids
        if installation_ids is not None
        else ["installation-" + "1" * 32],
        "operations": operations
        if operations is not None
        else ["activate", "deactivate", "recover"],
    }


@pytest.fixture
def updater(tmp_path: Path) -> updates.ComponentUpdater:
    catalog_path = tmp_path / "component-catalog.json"
    catalog_path.write_text(
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
        catalog_path=catalog_path,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "update-state",
        install_root=tmp_path / "install",
        broker_path=tmp_path / "missing-broker",
        trusted_catalog_digest=None,
        load_active_catalog=False,
    )


def _write_catalog(
    updater: updates.ComponentUpdater, sources: list[dict[str, Any]]
) -> dict[str, Any]:
    value = {"schema_version": 1, "generation": 7, "sources": sources}
    updater.activity_catalog_path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return value


def test_legacy_source_without_binding_scopes_remains_valid_and_denied_by_default(
    updater: updates.ComponentUpdater,
) -> None:
    catalog = _write_catalog(updater, [dict(PACKAGE_SOURCE)])
    loaded, source_ids = updater._activity_catalog()
    assert loaded == catalog
    assert source_ids == ["cyrene-yield"]
    assert "binding_scopes" not in loaded["sources"][0]


def test_empty_binding_scopes_are_valid_but_authorize_nothing(
    updater: updates.ComponentUpdater,
) -> None:
    catalog = _write_catalog(updater, [{**PACKAGE_SOURCE, "binding_scopes": []}])
    loaded, source_ids = updater._activity_catalog()
    assert loaded == catalog
    assert source_ids == ["cyrene-yield"]
    assert loaded["sources"][0]["binding_scopes"] == []


def test_valid_exact_installation_scope_is_preserved_for_broker_generation(
    updater: updates.ComponentUpdater,
) -> None:
    expected = _scope()
    catalog = _write_catalog(updater, [{**PACKAGE_SOURCE, "binding_scopes": [expected]}])
    loaded, source_ids = updater._activity_catalog()
    assert loaded == catalog
    assert loaded["generation"] == 7
    assert loaded["sources"][0]["binding_scopes"] == [expected]
    assert source_ids == ["cyrene-yield"]


@pytest.mark.parametrize(
    "scopes",
    [
        None,
        "not-an-array",
        [{}],
        [{**_scope(), "operations": ["activate", "install"]}],
        [{**_scope(), "operations": ["activate", "activate"]}],
        [{**_scope(), "installation_ids": []}],
        [{**_scope(), "installation_ids": ["*"]}],
        [
            {
                **_scope(),
                "installation_ids": ["installation-" + "1" * 32, "installation-" + "1" * 32],
            }
        ],
        [{**_scope(), "binding_id": "../other"}],
        [{**_scope(), "package_id": "bad package id"}],
        [_scope(binding_id="yield.z"), _scope(binding_id="yield.a")],
        [_scope(), _scope()],
    ],
)
def test_rejects_malformed_or_ambiguous_optional_binding_scopes(
    updater: updates.ComponentUpdater, scopes: Any
) -> None:
    source = {**PACKAGE_SOURCE, "binding_scopes": scopes}
    _write_catalog(updater, [source])
    with pytest.raises(updates.UpdateError) as error:
        updater._activity_catalog()
    assert error.value.code == "GATE_UNKNOWN"


def test_rejects_unknown_scope_fields_and_boolean_source_trust_values(
    updater: updates.ComponentUpdater,
) -> None:
    extra_field = {**_scope(), "installation_glob": "*"}
    _write_catalog(updater, [{**PACKAGE_SOURCE, "binding_scopes": [extra_field]}])
    with pytest.raises(updates.UpdateError, match="malformed binding scope"):
        updater._activity_catalog()

    invalid_source = {**PACKAGE_SOURCE, "uid": True, "binding_scopes": []}
    _write_catalog(updater, [invalid_source])
    with pytest.raises(updates.UpdateError, match="invalid trust metadata"):
        updater._activity_catalog()


@pytest.mark.parametrize(
    "catalog_changes",
    [
        {"schema_version": True},
        {"unknown": "field"},
    ],
)
def test_rejects_noncanonical_catalog_envelope(
    updater: updates.ComponentUpdater, catalog_changes: dict[str, Any]
) -> None:
    catalog = {"schema_version": 1, "generation": 7, "sources": [dict(PACKAGE_SOURCE)]}
    catalog.update(catalog_changes)
    updater.activity_catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(updates.UpdateError) as error:
        updater._activity_catalog()
    assert error.value.code == "GATE_UNKNOWN"


def test_contract_lock_validator_keeps_exact_existing_and_package_runtime_paths() -> None:
    base = {
        "repository": "DoHorizon-AI/Cyrene-Workspace",
        "commit": "e75b0ea17b93bf7479f8e5571e4b4313c788115b",
        "sha256": "sha256:" + "a" * 64,
    }
    assert updates._trusted_contract_lock(
        {
            **base,
            "path": "governance/workspace-connection-protocols-v2.lock.json",
        }
    )
    assert updates._trusted_contract_lock(
        {
            **base,
            "path": "governance/package-runtime-protocols-v1.lock.json",
            "sha256": "sha256:64904fc3e7a46025fc7e6813e597471763027b46dda9783a17a4c712b6fdf941",
        }
    )
    for changed in (
        {**base, "path": "governance/other.lock.json"},
        {
            **base,
            "repository": "attacker/Workspace",
            "path": "governance/package-runtime-protocols-v1.lock.json",
        },
        {
            **base,
            "commit": "not-a-commit",
            "path": "governance/package-runtime-protocols-v1.lock.json",
        },
    ):
        assert not updates._trusted_contract_lock(changed)
