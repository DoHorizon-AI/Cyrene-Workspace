"""
┌─────────────────────────────────────────────────────────────────────────────┐
│  📄 test_sync_logging_spec.py                                               │
│  Module: tests.test_sync_logging_spec                                       │
│  Role: Unit and integration tests for logging spec synchronization script.  │
│                                                                             │
│  测试职责：验证 Cyrene 日志与错误码规范同步脚本能够正确发现所有仓库、        │
│  检测同步漂移（--check）、并在执行后保持跨仓一致性。                        │
└─────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "sync-logging-spec.py"

spec = importlib.util.spec_from_file_location("sync_logging_spec", SCRIPT_PATH)
assert spec and spec.loader
sync_logging_spec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_logging_spec)


def test_canonical_spec_exists() -> None:
    """Verifies that the canonical spec file exists in Cyrene-Workspace/docs/standards/.

    中文：验证 Workspace 的 docs/standards/ 目录中存在规范日志文件。
    """
    canonical_file = sync_logging_spec.CANONICAL_SPEC_FILE
    assert canonical_file.exists(), f"Canonical spec missing at {canonical_file}"
    content = canonical_file.read_text(encoding="utf-8")
    assert "Cyrene 日志、错误码与诊断规范" in content
    assert "REVIEW_READY" in content


def test_repository_discovery() -> None:
    """Verifies that discovery finds at least all 10 core repositories.

    中文：验证仓库发现结果至少覆盖全部 10 个核心仓库。
    """
    targets = sync_logging_spec.discover_repositories()
    assert len(targets) >= 10
    names = {t.name for t in targets}
    expected_repos = {
        "Cyrene-Platform",
        "Cyrene-Plugins-Official",
        "Cyrene-Studio",
        "Cyrene-Workspace",
        "Cyrene-Catalyst",
        "Cyrene-Echo",
        "Cyrene-Exchange",
        "Cyrene-Navigator",
        "Cyrene-Reactor",
        "Cyrene-Yield",
    }
    assert expected_repos.issubset(names)


def test_sync_check_passes() -> None:
    """Verifies that all repositories are currently synchronized.

    中文：验证当前所有仓库的规范副本均已同步。
    """
    result = sync_logging_spec.sync_specs(check_only=True)
    assert result == 0, "Sync check must return 0 when all repositories are synchronized"
