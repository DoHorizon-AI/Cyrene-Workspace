"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_prepare_production_runtime.py                                    │
│  Module: tooling.deploy.teams_cloud.tests.test_prepare_production_runtime│
│  Role: Verify the image-stage production dependency carrier.             │
│                                                                          │
│  模块职责：验证镜像构建阶段生成的生产依赖载体。                         │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
runtime_package = importlib.import_module("prepare_production_runtime")


def test_runtime_package_converts_dsh_peers_and_excludes_build_caches(tmp_path: Path) -> None:
    """Deploy only compiled Navigator files and the pinned DSH runtime closure."""

    upstream = tmp_path / "deepseek-harness"
    (upstream / "packages" / "core" / "tools").mkdir(parents=True)
    (upstream / "packages" / "core" / "scope").mkdir(parents=True)
    (upstream / "packages" / "core" / "runtime").mkdir(parents=True)
    (upstream / "pnpm-workspace.yaml").write_text("packages: [packages/*/*]\n", encoding="utf-8")
    (upstream / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (upstream / "packages" / "core" / "tools" / "package.json").write_text(
        json.dumps(
            {
                "name": "@deepseek-ai/dsh-tools",
                "peerDependencies": {"@deepseek-ai/dsh-scope": "workspace:*"},
                "dependencies": {"@deepseek-ai/dsh-runtime": "workspace:*"},
            }
        ),
        encoding="utf-8",
    )
    for name, root in (
        ("dsh-scope", "scope"),
        ("dsh-runtime", "runtime"),
    ):
        (upstream / "packages" / "core" / root / "package.json").write_text(
            json.dumps({"name": f"@deepseek-ai/{name}"}), encoding="utf-8"
        )

    harness = tmp_path / "harness"
    (harness / "dist" / "workflows").mkdir(parents=True)
    (harness / "presets").mkdir()
    (harness / "src").mkdir()
    (harness / "tests").mkdir()
    (harness / "node_modules" / "@deepseek-ai").mkdir(parents=True)
    (harness / "dist" / "serve.js").write_text("export {};\n", encoding="utf-8")
    (harness / "dist" / "workflows" / "runtime.js").write_text("export {};\n", encoding="utf-8")
    (harness / "presets" / "README.md").write_text("runtime preset\n", encoding="utf-8")
    (harness / "tests" / "fixture.test.mjs").write_text("test\n", encoding="utf-8")
    (harness / "node_modules" / "@deepseek-ai" / "dsh-tools").write_text(
        "symlink target\n", encoding="utf-8"
    )
    (harness / "cordis.patch.yml").write_text("plugins: []\n", encoding="utf-8")
    (harness / "src" / "serve.ts").write_text(
        "import SessionProjection from '@deepseek-ai/dsh-session-projection';\n"
        "import SystemPrompt from '@deepseek-ai/dsh-system-prompt';\n",
        encoding="utf-8",
    )
    (harness / "package.json").write_text(
        json.dumps(
            {
                "name": "@cyrene/navigator-harness",
                "version": "0.1.0",
                "type": "module",
                "exports": {".": "./dist/index.js"},
                "peerDependencies": {
                    "@deepseek-ai/cordis": "4.0.4",
                    "@deepseek-ai/dsh-tools": "0.2.0-rc.2",
                },
                "devDependencies": {"test-only": "1.0.0"},
            }
        ),
        encoding="utf-8",
    )

    destination = runtime_package.prepare_runtime_package(upstream, harness)
    manifest = json.loads((destination / "package.json").read_text(encoding="utf-8"))

    assert manifest["dependencies"]["@deepseek-ai/cordis"] == "workspace:~"
    assert manifest["dependencies"]["@deepseek-ai/dsh-tools"] == "workspace:*"
    assert manifest["dependencies"]["@deepseek-ai/dsh-base"] == "workspace:*"
    assert manifest["dependencies"]["@deepseek-ai/dsh-schedule"] == "workspace:*"
    assert manifest["dependencies"]["@deepseek-ai/dsh-session-projection"] == "workspace:*"
    assert manifest["dependencies"]["@deepseek-ai/dsh-system-prompt"] == "workspace:*"
    assert manifest["dependencies"]["@deepseek-ai/dsh-scope"] == "workspace:*"
    assert manifest["dependencies"]["@deepseek-ai/dsh-runtime"] == "workspace:*"
    assert "peerDependencies" not in manifest
    assert "devDependencies" not in manifest
    assert not (destination / "tests").exists()
    assert not (destination / "node_modules").exists()


def test_runtime_package_rejects_non_navigator_manifest(tmp_path: Path) -> None:
    """Fail closed before adding a foreign or malformed package to the DSH workspace."""

    upstream = tmp_path / "deepseek-harness"
    (upstream / "packages").mkdir(parents=True)
    (upstream / "pnpm-workspace.yaml").write_text("packages: [packages/*/*]\n", encoding="utf-8")
    (upstream / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "package.json").write_text(
        json.dumps({"name": "@untrusted/other-package", "peerDependencies": {}, "exports": {}}),
        encoding="utf-8",
    )

    with pytest.raises(runtime_package.RuntimePackageError, match="does not match"):
        runtime_package.prepare_runtime_package(upstream, harness)
