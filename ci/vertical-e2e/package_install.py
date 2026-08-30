#!/usr/bin/env python3
"""Prepare an immutable Official OneBot package for the vertical acceptance.

The script deliberately consumes the lifecycle implementation from the exact
Plugins checkout cloned by the harness.  It builds, publishes, verifies,
installs, activates, upgrades, rolls back, and offline-restores the package,
then reports only paths under the disposable host root.  CES subsequently
launches the real generic worker from that installed package tree.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


PACKAGE_ID = "cyrene.connectors.onebot-v11"
VERSION = "0.1.0"
UPGRADE_VERSION = "0.2.0"


class MemoryTransport:
    """Transport seam used only for package-host activation checks."""

    def call(
        self,
        action: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float,
        cancellation: Any,
    ) -> dict[str, str]:
        del action, params, timeout_seconds, cancellation
        return {"message_id": "package-activation-check"}

    def close(self) -> None:
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, check=True, env=env)


def _runtime_python(runtime_root: Path) -> Path:
    return runtime_root / "venv" / "bin" / "python"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugins-root", type=Path, required=True)
    parser.add_argument("--host-root", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    args = parser.parse_args()

    plugins_root = args.plugins_root.resolve(strict=True)
    host_root = args.host_root.resolve()
    source_root = plugins_root / "plugins" / "connectors" / "onebot-v11"
    tools_root = plugins_root / "tools"
    sys.path.insert(0, str(tools_root))

    from connector_package import (  # noqa: PLC0415
        ConnectorPackageHost,
        LocalPackageRegistry,
        PackageLifecycleError,
        build_package,
    )

    build_root = host_root / "build"
    registry = LocalPackageRegistry(host_root / "registry")
    first = build_package(
        source_root,
        build_root / VERSION,
        source_revision=args.source_revision,
        include_sbom=True,
    )
    repeat = build_package(
        source_root,
        build_root / f"{VERSION}-repeat",
        source_revision=args.source_revision,
        include_sbom=True,
    )
    if first.archive_path.read_bytes() != repeat.archive_path.read_bytes():
        raise RuntimeError("package build is not byte-for-byte deterministic")
    if first.artifact_digest != repeat.artifact_digest:
        raise RuntimeError("package artifact identity is not deterministic")
    if b"latest" in first.archive_path.read_bytes().lower():
        raise RuntimeError("immutable package contains a floating latest dependency")
    registry.publish(first)

    upgrade = build_package(
        source_root,
        build_root / UPGRADE_VERSION,
        source_revision=args.source_revision,
        version_override=UPGRADE_VERSION,
        include_sbom=True,
    )
    registry.publish(upgrade)

    preparation_counts: dict[str, int] = defaultdict(int)
    prepared_lock_digests: dict[str, str] = {}

    def prepare_dependencies(lock_path: Path, runtime_root: Path) -> None:
        artifact_digest = runtime_root.name
        requirements = [
            line.strip()
            for line in lock_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not requirements or any(
            "==" not in requirement or "latest" in requirement.lower()
            for requirement in requirements
        ):
            raise RuntimeError("dependency preparation requires exact locked versions")
        lock_digest = _sha256(lock_path)
        marker = runtime_root / "prepared-lock.sha256"
        python = _runtime_python(runtime_root)
        if marker.is_file():
            if marker.read_text(encoding="utf-8").strip() != lock_digest:
                raise RuntimeError("dependency runtime lock digest changed")
            if not python.is_file():
                raise RuntimeError("prepared dependency runtime is incomplete")
            return
        _run(
            [str(args.uv), "venv", "--python", sys.executable, str(runtime_root / "venv")]
        )
        _run(
            [
                str(args.uv),
                "pip",
                "install",
                "--python",
                str(python),
                "--requirements",
                str(lock_path),
            ]
        )
        marker.write_text(f"{lock_digest}\n", encoding="utf-8")
        preparation_counts[artifact_digest] += 1
        prepared_lock_digests[artifact_digest] = lock_digest

    secrets = {
        "qq-main": "phase7-main-secret",
        "qq-secondary": "phase7-secondary-secret",
    }

    def provide_secrets(binding_id: str, refs: tuple[str, ...]) -> dict[str, str]:
        if refs != ("onebot-token",):
            raise RuntimeError(f"unexpected secret references for {binding_id}: {refs}")
        return {"access_token": secrets[binding_id]}

    host = ConnectorPackageHost(
        host_root / "host",
        registry,
        secret_provider=provide_secrets,
        dependency_installer=prepare_dependencies,
    )
    installed = host.install(PACKAGE_ID, VERSION)
    installed_again = host.install(PACKAGE_ID, VERSION)
    if installed.artifact_digest != installed_again.artifact_digest:
        raise RuntimeError("repeated install did not reuse the immutable artifact")
    if registry.download_count != 1:
        raise RuntimeError("one artifact was downloaded more than once for two bindings")
    if preparation_counts[first.artifact_digest] != 1:
        raise RuntimeError("locked dependencies were not prepared exactly once per digest")

    def binding_config(account_id: str) -> dict[str, Any]:
        return {
            "http_base_url": "http://127.0.0.1:1",
            "runtime_profile": "onebot-v11",
            "self_account_id": account_id,
            "timeout_seconds": 1.0,
        }

    for binding_id, account_id in (("qq-main", "10001"), ("qq-secondary", "10002")):
        host.create_binding(
            binding_id,
            PACKAGE_ID,
            VERSION,
            binding_config(account_id),
            secret_refs=("onebot-token",),
        )
        instance = host.activate_binding(binding_id, transport=MemoryTransport())
        if instance.configured_binding_id != binding_id:
            raise RuntimeError("package activation lost configured binding identity")
        if instance._config.access_token != secrets[binding_id]:
            raise RuntimeError("binding secret resolution crossed configured instances")
        class_path = Path(inspect.getfile(instance.__class__)).resolve(strict=True)
        if not class_path.is_relative_to(host.installed_root.resolve(strict=True)):
            raise RuntimeError("package runtime loaded from a source checkout")

    if host.runtime_class_load_count != 1:
        raise RuntimeError("two bindings did not reuse package runtime code")
    main_generation = host.binding_record("qq-main")["runtime_generation"]
    host.restart_binding("qq-main", transport=MemoryTransport())
    restarted = host.binding_record("qq-main")
    if restarted["binding_id"] != "qq-main":
        raise RuntimeError("restart changed configured binding identity")
    if restarted["runtime_generation"] != main_generation + 1:
        raise RuntimeError("restart did not advance runtime generation")

    host.upgrade_binding(
        "qq-main", PACKAGE_ID, UPGRADE_VERSION, transport=MemoryTransport()
    )
    if host.binding_record("qq-secondary")["package_version"] != VERSION:
        raise RuntimeError("binding-scoped upgrade changed another binding")
    host.rollback_binding("qq-main", transport=MemoryTransport())
    if host.binding_record("qq-main")["package_version"] != VERSION:
        raise RuntimeError("rollback did not recover the original binding package")
    if preparation_counts[upgrade.artifact_digest] != 1:
        raise RuntimeError("upgrade dependencies were not prepared once per digest")
    primary_downloads = registry.download_count
    if primary_downloads != 2:
        raise RuntimeError("primary host did not download exactly one artifact per digest")

    state_text = (host.state_root / "bindings.json").read_text(encoding="utf-8")
    if any(value in state_text for value in secrets.values()):
        raise RuntimeError("binding secret value was persisted in package host state")

    # A separate clean host proves verified offline reconstruction and rejects a
    # corrupted cache without disturbing the bindings used by the vertical run.
    offline_host = ConnectorPackageHost(host_root / "offline-host", registry)
    offline_host.install(PACKAGE_ID, VERSION)
    offline_host.uninstall(PACKAGE_ID, VERSION)
    cache_archive = offline_host._cache_archive_path(first.artifact_digest)
    cache_archive.write_bytes(b"corrupted-package-cache")
    try:
        offline_host.install_offline(PACKAGE_ID, VERSION)
    except PackageLifecycleError as error:
        if error.code != "INTEGRITY_FAILURE":
            raise
    else:
        raise RuntimeError("corrupted offline package cache was accepted")
    cache_archive.write_bytes(first.archive_path.read_bytes())
    restored = offline_host.install_offline(PACKAGE_ID, VERSION)
    if restored.artifact_digest != first.artifact_digest:
        raise RuntimeError("offline reinstall changed immutable artifact identity")

    host.close_binding("qq-main")
    host.close_binding("qq-secondary")
    installed_root = host.installed_root / PACKAGE_ID / VERSION
    runtime_python = _runtime_python(
        host.root / "runtimes" / first.artifact_digest
    )
    result = {
        "package_id": PACKAGE_ID,
        "package_version": VERSION,
        "artifact_digest": f"sha256:{first.artifact_digest}",
        "archive_digest": f"sha256:{first.archive_digest}",
        "installed_root": str(installed_root.resolve(strict=True)),
        "manifest": str((installed_root / "plugin.manifest.json").resolve(strict=True)),
        # Keep the host-owned venv path instead of resolving its interpreter
        # symlink to /usr/bin/python.  CES must be configured through the
        # package runtime identity, even though that venv ultimately uses the
        # host's immutable base interpreter binary.
        "runtime_python": str(runtime_python.absolute()),
        "registry_downloads_for_primary_host": primary_downloads,
        "dependency_preparations": dict(sorted(preparation_counts.items())),
        "prepared_lock_digests": dict(sorted(prepared_lock_digests.items())),
        "bindings": [host.binding_record("qq-main"), host.binding_record("qq-secondary")],
        "offline_reinstall": "PASS",
        "corrupt_cache_rejection": "PASS",
        "upgrade_rollback": "PASS",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"PACKAGE_INSTALL_RESULT={args.output}")
    print(f"PACKAGE_ARTIFACT_DIGEST={result['artifact_digest']}")
    print("PACKAGE_LIFECYCLE_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
