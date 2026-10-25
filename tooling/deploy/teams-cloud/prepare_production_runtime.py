"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 prepare_production_runtime.py                                        │
│  Module: tooling.deploy.teams_cloud.prepare_production_runtime           │
│  Role: Create the minimal pinned DSH/Navigator runtime workspace package.│
│                                                                          │
│  模块职责：生成锁定的 DSH/Navigator 最小生产运行时工作区包。            │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any


class RuntimePackageError(ValueError):
    """The selected Harness package cannot form the pinned production closure."""


def _workspace_specifier(name: str) -> str:
    """Return the local workspace range for one approved DSH peer package."""

    return (
        "workspace:~"
        if name in {"@deepseek-ai/cordis", "@deepseek-ai/schemastery"}
        else "workspace:*"
    )


def _runtime_imports(harness_root: Path) -> set[str]:
    """Find DSH packages imported by runtime TypeScript/JavaScript source."""

    package_names: set[str] = set()
    source_root = harness_root / "src"
    if not source_root.is_dir():
        raise RuntimePackageError("Navigator Harness runtime source is missing")
    package_pattern = re.compile(r"['\"](@deepseek-ai/[^'\"]+)['\"]")
    for source in source_root.rglob("*"):
        if not source.is_file() or source.suffix not in {".ts", ".tsx", ".js", ".mjs"}:
            continue
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise RuntimePackageError("Navigator Harness runtime source is unreadable") from error
        for specifier in package_pattern.findall(content):
            package_names.add("/".join(specifier.split("/")[:2]))
    return package_names


def _workspace_dependency_closure(upstream_root: Path, roots: set[str]) -> set[str]:
    """Resolve workspace dependencies and required peers without including dev tools."""

    manifests: dict[str, dict[str, Any]] = {}
    for current, directories, files in os.walk(upstream_root):
        directories[:] = [
            name for name in directories if name not in {"node_modules", ".git", ".pnpm"}
        ]
        if "package.json" not in files:
            continue
        path = Path(current) / "package.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        name = document.get("name") if isinstance(document, dict) else None
        if isinstance(name, str) and name.startswith("@deepseek-ai/"):
            if name in manifests:
                raise RuntimePackageError(f"upstream contains duplicate workspace package {name}")
            manifests[name] = document

    pending = list(roots)
    included: set[str] = set()
    while pending:
        name = pending.pop()
        if name in included:
            continue
        included.add(name)
        document = manifests.get(name)
        if document is None:
            continue
        for field in ("dependencies", "optionalDependencies", "peerDependencies"):
            entries = document.get(field, {})
            if not isinstance(entries, dict):
                raise RuntimePackageError(f"workspace package {name} has an invalid {field}")
            for dependency in entries:
                if not isinstance(dependency, str):
                    raise RuntimePackageError(f"workspace package {name} has an invalid dependency")
                peer_meta = document.get("peerDependenciesMeta", {})
                optional_peer = (
                    field == "peerDependencies"
                    and isinstance(peer_meta, dict)
                    and isinstance(peer_meta.get(dependency), dict)
                    and peer_meta[dependency].get("optional") is True
                )
                if dependency in manifests and not optional_peer:
                    pending.append(dependency)
    return {name for name in included if name in manifests}


def prepare_runtime_package(upstream_root: Path, harness_root: Path) -> Path:
    """Copy compiled Navigator files and make DSH peers explicit runtime dependencies.

    The generated package lives only in the image build stage. Its dependency list
    lets pnpm deploy the DSH production closure instead of copying the full monorepo
    node_modules tree into the final ARM64 image.
    """

    upstream = upstream_root.resolve(strict=True)
    harness = harness_root.resolve(strict=True)
    if (
        not (upstream / "pnpm-workspace.yaml").is_file()
        or not (upstream / "pnpm-lock.yaml").is_file()
    ):
        raise RuntimePackageError("upstream Harness workspace and lockfile are required")
    source_manifest_path = harness / "package.json"
    try:
        source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimePackageError("Navigator Harness package manifest is invalid") from error
    if (
        not isinstance(source_manifest, dict)
        or source_manifest.get("name") != "@cyrene/navigator-harness"
        or not isinstance(source_manifest.get("peerDependencies"), dict)
        or not isinstance(source_manifest.get("exports"), dict)
    ):
        raise RuntimePackageError("Navigator Harness package does not match the runtime contract")

    direct_runtime_imports = _runtime_imports(harness)
    destination = upstream / "packages" / "cyrene" / "navigator-harness"
    if destination.exists() or destination.is_symlink():
        raise RuntimePackageError("generated Navigator Harness package path already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        harness,
        destination,
        ignore=shutil.ignore_patterns("node_modules", "tests", "*.tsbuildinfo", ".upstream"),
    )

    manifest_path = destination / "package.json"
    manifest: dict[str, Any] = source_manifest
    dependencies = manifest.get("dependencies", {})
    if not isinstance(dependencies, dict):
        raise RuntimePackageError("Navigator Harness dependencies must be an object")
    dependencies = dict(dependencies)
    for name in manifest["peerDependencies"]:
        if not isinstance(name, str) or not name.startswith("@deepseek-ai/"):
            raise RuntimePackageError("Navigator Harness declares an unapproved peer dependency")
        dependencies[name] = _workspace_specifier(name)

    for name in direct_runtime_imports:
        dependencies[name] = _workspace_specifier(name)

    # Navigator imports these packages directly; keep them explicit even when a
    # future DSH base patch removes their transitive dependency edge.
    for name in (
        "@deepseek-ai/dsh-agent-loop",
        "@deepseek-ai/dsh-base",
        "@deepseek-ai/dsh-schedule",
        "@deepseek-ai/dsh-subprocess-local",
    ):
        dependencies[name] = "workspace:*"

    runtime_roots = set(dependencies)
    dependencies.update(
        {
            name: _workspace_specifier(name)
            for name in _workspace_dependency_closure(upstream, runtime_roots)
        }
    )
    manifest["dependencies"] = dependencies
    manifest.pop("peerDependencies", None)
    manifest.pop("devDependencies", None)
    manifest["files"] = ["dist", "presets", "cordis.patch.yml"]
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    for required in ("dist/serve.js", "dist/workflows/runtime.js", "presets"):
        if not (destination / required).exists():
            raise RuntimePackageError(f"compiled Navigator runtime is missing {required}")
    return destination


def main() -> int:
    """Prepare one temporary production package for the Docker build stage."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--harness", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        destination = prepare_runtime_package(arguments.upstream, arguments.harness)
    except (OSError, RuntimePackageError) as error:
        print(f"Production runtime packaging failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"package": "@cyrene/navigator-harness", "path": str(destination)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
