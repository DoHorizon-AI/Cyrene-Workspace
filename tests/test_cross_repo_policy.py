"""
┌───────────────────────────────────────────────────────────────────────────────┐
│  📄 test_cross_repo_policy.py                                                  │
│  Module: tests.test_cross_repo_policy                                         │
│  Role: Unit and integration tests for cross-repository naming & boundary      │
│        policy verifier (GOV-001).                                             │
│                                                                               │
│  测试职责：验证跨仓命名与边界门禁能够从 Platform exact SHA 读取政策，对违规代码   │
│  精准报警，并保证当前六大 Product 仓库真实源码 100% 遵从规约。                    │
└───────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# The verifier is an executable Workspace script rather than an installed package.
SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from cross_repo_boundary_verifier import (
    CrossRepoBoundaryVerifier,
    PolicyAuthority,
)

PLATFORM_DIR = (Path(__file__).parents[2] / "Cyrene-Platform").resolve()


def test_platform_exact_authority_loaded() -> None:
    """Verifies that PolicyAuthority directly loads from Platform exact SHA without hardcoded vocabulary."""
    authority = PolicyAuthority.load(PLATFORM_DIR)
    assert len(authority.exact_sha) == 40, "Must load full 40-character commit SHA"
    assert authority.policy_version >= 1
    assert authority.canonical_document == "docs/governance/API_NAMING_CONSTITUTION.md"
    assert len(authority.forbidden_symbols) == 13
    assert len(authority.forbidden_patterns) == 13
    assert "AcquireSemanticLeaseRequest" in authority.forbidden_symbols
    assert "ReserveResourcesRequest" in authority.forbidden_symbols


def test_verifier_detects_forbidden_naming_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that a forbidden naming symbol is detected with line number and snippet."""
    mock_repo_dir = tmp_path / "Cyrene-MockService"
    mock_src_dir = mock_repo_dir / "src"
    mock_src_dir.mkdir(parents=True)

    bad_file = mock_src_dir / "service.py"
    bad_file.write_text(
        "import sys\n\n# Prohibited call\nreq = AcquireSemanticLeaseRequest()\n",
        encoding="utf-8",
    )

    mock_manifest = {
        "repositories": [
            {
                "name": "Cyrene-MockService",
                "path": str(mock_repo_dir),
                "pointers": {
                    "primary_source_roots": [str(mock_src_dir)],
                },
            }
        ]
    }
    manifest_file = tmp_path / "repositories.yaml"
    manifest_file.write_text(yaml.safe_dump(mock_manifest), encoding="utf-8")

    verifier = CrossRepoBoundaryVerifier(
        workspace_root=tmp_path,
        platform_path=PLATFORM_DIR,
    )
    result = verifier.verify(target_repos=["Cyrene-MockService"])

    assert not result.passed
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.repository == "Cyrene-MockService"
    assert v.rule_name == "AcquireSemanticLeaseRequest"
    assert v.line_number == 4
    assert "req = AcquireSemanticLeaseRequest()" in v.offending_line


def test_verifier_detects_kernel_internal_coupling(tmp_path: Path) -> None:
    """Verifies that direct coupling to Platform cy_kernel_daemon source is flagged."""
    mock_repo_dir = tmp_path / "Cyrene-MockService"
    mock_src_dir = mock_repo_dir / "src"
    mock_src_dir.mkdir(parents=True)

    bad_file = mock_src_dir / "client.py"
    bad_file.write_text(
        "from cy_kernel_daemon import DaemonManager\n",
        encoding="utf-8",
    )

    mock_manifest = {
        "repositories": [
            {
                "name": "Cyrene-MockService",
                "path": str(mock_repo_dir),
                "pointers": {
                    "primary_source_roots": [str(mock_src_dir)],
                },
            }
        ]
    }
    manifest_file = tmp_path / "repositories.yaml"
    manifest_file.write_text(yaml.safe_dump(mock_manifest), encoding="utf-8")

    verifier = CrossRepoBoundaryVerifier(
        workspace_root=tmp_path,
        platform_path=PLATFORM_DIR,
    )
    result = verifier.verify(target_repos=["Cyrene-MockService"])

    assert not result.passed
    assert len(result.violations) == 1
    assert result.violations[0].rule_name == "kernel_daemon_direct_coupling"


def test_verifier_detects_retired_service_json(tmp_path: Path) -> None:
    """Verifies that re-introducing retired service.json in Navigator is flagged."""
    mock_nav_dir = tmp_path / "Cyrene-Navigator"
    mock_nav_dir.mkdir(parents=True)
    (mock_nav_dir / "service.json").write_text("{}", encoding="utf-8")
    mock_src = mock_nav_dir / "src"
    mock_src.mkdir()
    (mock_src / "main.py").write_text("print('ok')", encoding="utf-8")

    mock_manifest = {
        "repositories": [
            {
                "name": "Cyrene-Navigator",
                "path": str(mock_nav_dir),
                "pointers": {
                    "primary_source_roots": [str(mock_src)],
                },
            }
        ]
    }
    manifest_file = tmp_path / "repositories.yaml"
    manifest_file.write_text(yaml.safe_dump(mock_manifest), encoding="utf-8")

    verifier = CrossRepoBoundaryVerifier(
        workspace_root=tmp_path,
        platform_path=PLATFORM_DIR,
    )
    result = verifier.verify(target_repos=["Cyrene-Navigator"])

    assert not result.passed
    assert any(v.rule_name == "prohibited_legacy_file" for v in result.violations)


def test_all_six_product_repositories_live_conformance() -> None:
    """Integration gate: Verifies that all 6 live Product repositories pass naming and boundary policy."""
    workspace_root = Path(__file__).parents[1]
    verifier = CrossRepoBoundaryVerifier(
        workspace_root=workspace_root,
        platform_path=PLATFORM_DIR,
    )
    result = verifier.verify()

    assert result.passed, (
        f"Expected 6 product services to pass, but found violations: {result.violations}"
    )
    assert result.total_files_scanned > 120, "Should scan a realistic production source tree"
    assert set(result.repositories_checked) == {
        "Cyrene-Reactor",
        "Cyrene-Yield",
        "Cyrene-Exchange",
        "Cyrene-Catalyst",
        "Cyrene-Echo",
        "Cyrene-Navigator",
    }
