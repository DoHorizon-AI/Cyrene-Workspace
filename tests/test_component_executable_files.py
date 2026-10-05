"""Signed native executable metadata and extraction mode regressions."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
UPDATES_PATH = WORKSPACE_ROOT / "packaging" / "component_updates.py"
spec = importlib.util.spec_from_file_location("cyrene_executable_files_test", UPDATES_PATH)
assert spec is not None and spec.loader is not None
updates = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = updates
spec.loader.exec_module(updates)


def _artifact(**overrides: object) -> dict[str, object]:
    artifact: dict[str, object] = {
        "entrypoint": "bin/authority-host",
        "files": {
            "bin/authority-host": "sha256:" + "1" * 64,
            "bin/cy-workspace-authority-admin": "sha256:" + "2" * 64,
            "share/authority/config.json": "sha256:" + "3" * 64,
        },
    }
    artifact.update(overrides)
    return artifact


def test_legacy_native_manifest_keeps_only_entrypoint_executable() -> None:
    assert updates._native_executable_files(_artifact()) == {"bin/authority-host"}


def test_explicit_executable_files_are_normalized_signed_payload_paths() -> None:
    artifact = _artifact(executableFiles=["bin/authority-host", "bin/cy-workspace-authority-admin"])
    assert updates._native_executable_files(artifact) == {
        "bin/authority-host",
        "bin/cy-workspace-authority-admin",
    }


def test_entrypoint_must_be_in_signed_file_map() -> None:
    artifact = _artifact(files={"share/config.json": "sha256:" + "3" * 64})
    with pytest.raises(updates.UpdateError, match="entrypoint is not a signed file"):
        updates._native_executable_files(artifact)


@pytest.mark.parametrize(
    "executable_files",
    [
        None,
        "bin/authority-host",
        [],
        ["bin/authority-host", "bin/authority-host"],
        ["bin/authority-host", "../outside"],
        ["bin/authority-host", "bin/not-in-files"],
        ["bin/cy-workspace-authority-admin"],
        ["bin/authority-host", "bin\\admin"],
    ],
)
def test_invalid_executable_files_fail_closed(executable_files: object) -> None:
    artifact = _artifact(executableFiles=executable_files)
    with pytest.raises(updates.UpdateError) as error:
        updates._native_executable_files(artifact)
    assert error.value.code == "INVALID_MANIFEST"


def test_payload_modes_follow_signed_list_not_archive_modes(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "authority-host"
    helper = tmp_path / "bin" / "authority-admin"
    static = tmp_path / "share" / "config.json"
    for path in (executable, helper, static):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("payload", encoding="utf-8")
        path.chmod(0o777)

    updates.ComponentUpdater._normalize_payload(
        tmp_path, {"bin/authority-host", "bin/authority-admin"}
    )

    assert executable.stat().st_mode & 0o777 == 0o755
    assert helper.stat().st_mode & 0o777 == 0o755
    assert static.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    "schema_name",
    [
        "component-release-manifest-v1.schema.json",
        "component-release-manifest-v2.schema.json",
    ],
)
def test_release_schemas_declare_optional_unique_safe_executable_paths(schema_name: str) -> None:
    schema = json.loads((WORKSPACE_ROOT / "governance" / schema_name).read_text())
    executable_files = schema["$defs"]["nativeBinaryArtifact"]["properties"]["executableFiles"]
    assert executable_files["type"] == "array"
    assert executable_files["minItems"] == 1
    assert executable_files["uniqueItems"] is True
    assert executable_files["items"] == {"$ref": "#/$defs/relativePath"}
