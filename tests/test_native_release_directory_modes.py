"""Native release directories stay traversable under restrictive umasks."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
UPDATES_PATH = WORKSPACE_ROOT / "packaging" / "component_updates.py"
spec = importlib.util.spec_from_file_location("native_release_directory_test", UPDATES_PATH)
assert spec is not None and spec.loader is not None
updates = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = updates
spec.loader.exec_module(updates)


def test_native_release_ancestors_are_traversable_with_private_umask(tmp_path: Path) -> None:
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
    updater = updates.ComponentUpdater(
        catalog_path=catalog_path,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "update-state",
        install_root=tmp_path / "install",
        broker_path=tmp_path / "missing-broker",
        trusted_catalog_digest=None,
        load_active_catalog=False,
    )

    original_umask = os.umask(0o077)
    try:
        updater._ensure_native_release_directories("cyrene-test")
    finally:
        os.umask(original_umask)

    directories = (
        updater.install_root,
        updater.install_root / "components",
        updater.install_root / "components" / "cyrene-test",
        updater.install_root / "components" / "cyrene-test" / "releases",
    )
    for directory in directories:
        metadata = directory.lstat()
        assert stat.S_ISDIR(metadata.st_mode)
        assert metadata.st_uid == os.geteuid()
        assert stat.S_IMODE(metadata.st_mode) == 0o755
