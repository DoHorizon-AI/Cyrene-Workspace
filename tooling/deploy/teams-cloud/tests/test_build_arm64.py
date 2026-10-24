"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_build_arm64.py                                                  │
│  Module: tooling.deploy.teams_cloud.tests.test_build_arm64              │
│  Role: Verify source and image-reference lock requirements.              │
│                                                                          │
│  模块职责：验证源码及镜像引用锁定要求。                                  │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
build = __import__("build_arm64")


def _plan() -> dict[str, Any]:
    """Build the smallest valid plan with immutable ARM64 image indexes."""

    commit = "a" * 40
    digest = "b" * 64
    return {
        "schemaVersion": "cyrene-teams-cloud-build-v1",
        "taskId": "cloud-task-20261023",
        "targetPlatform": "linux/arm64",
        "sources": {
            "workspace": {
                "remote": "https://github.com/DoHorizon-AI/Cyrene-Workspace.git",
                "commit": commit,
                "checkoutPath": "Cyrene-Workspace",
            }
        },
        "images": [
            {
                "service": "runtime",
                "dockerfile": "Cyrene-Workspace/Dockerfile",
                "context": ".",
                "sourceRepos": ["workspace"],
                "sourceLabelRepo": "workspace",
                "buildArgs": {"PYTHON_BASE_IMAGE": f"python:3.12-slim@sha256:{digest}"},
            }
        ],
        "externalImages": {"POSTGRES": f"postgres:16-bookworm@sha256:{digest}"},
    }


def test_build_plan_requires_digest_pinned_base_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept immutable image references and reject floating version tags."""

    plan_path = tmp_path / "build-plan.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    monkeypatch.setattr(build, "_private_artifact", lambda _path: True)
    assert build.validate_plan(plan_path)["targetPlatform"] == "linux/arm64"

    plan = _plan()
    plan["images"][0]["buildArgs"]["PYTHON_BASE_IMAGE"] = "python:3.12-slim"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(build.BuildError, match="pin a registry digest"):
        build.validate_plan(plan_path)


def test_build_plan_rejects_tag_only_external_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Require the runtime services' Postgres and Caddy images to be immutable too."""

    plan = _plan()
    plan["externalImages"]["POSTGRES"] = "postgres:16-bookworm"
    plan_path = tmp_path / "build-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(build, "_private_artifact", lambda _path: True)
    with pytest.raises(build.BuildError, match="pin a registry digest"):
        build.validate_plan(plan_path)
