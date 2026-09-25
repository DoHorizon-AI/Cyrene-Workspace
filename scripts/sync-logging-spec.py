#!/usr/bin/env python3
"""
┌─────────────────────────────────────────────────────────────────────────────┐
│ 📄 sync-logging-spec.py                                                     │
│ Module: scripts.sync-logging-spec                                           │
│ Role: Synchronize Cyrene Logging, Error Codes & Diagnostics Specification    │
│       to the root docs/ directory of all repositories in the ecosystem.     │
│                                                                             │
│ 模块职责：将 Cyrene 日志、错误码与诊断规范从 Cyrene-Workspace 权威源同步至   │
│ 各仓库根目录下的 docs/ 文件夹中，支持 --check 校验与自动化更新。            │
└─────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DEV_ROOT = WORKSPACE_ROOT.parent
CANONICAL_SPEC_REL = Path("docs/standards/logging-and-errors.md")
CANONICAL_SPEC_FILE = WORKSPACE_ROOT / CANONICAL_SPEC_REL

SYNC_HEADER = """<!--
================================================================================
SYNCHRONIZED DOCUMENT - DO NOT EDIT DIRECTLY IN THIS REPOSITORY
Canonical Source: Cyrene-Workspace/docs/standards/logging-and-errors.md
Synchronized By: scripts/sync-logging-spec.py
================================================================================
-->

"""


class TargetRepo(NamedTuple):
    name: str
    docs_dir: Path
    target_file: Path


def discover_repositories() -> list[TargetRepo]:
    """Discover all repositories and their docs directories.

    中文:发现所有仓库及其 docs 目录。
    """
    repo_dirs: dict[str, Path] = {}

    # 1. Attempt discovery via repositories.yaml if PyYAML is available
    # 中文:1. 如果已安装 PyYAML,则尝试从 repositories.yaml 中发现仓库
    # 中文:1. 如果 PyYAML 可用,先尝试通过 repositories.yaml 发现仓库。
    yaml_file = WORKSPACE_ROOT / "repositories.yaml"
    if yaml is not None and yaml_file.exists():
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            for repo in data.get("repositories", []):
                name = repo.get("name")
                rel_path = repo.get("path")
                if name and rel_path:
                    abs_path = (WORKSPACE_ROOT / rel_path).resolve()
                    if abs_path.exists():
                        repo_dirs[name] = abs_path
        except Exception as err:
            print(f"Warning: Failed to parse repositories.yaml: {err}", file=sys.stderr)

    # 2. Add known default paths if not discovered
    # 中文:2. 如果未发现仓库,则添加已知的默认路径
    # 中文:2. 对尚未发现的仓库补充已知默认路径。
    known_paths = [
        ("Cyrene-Workspace", WORKSPACE_ROOT),
        ("Cyrene-Platform", DEV_ROOT / "Cyrene-Platform"),
        ("Cyrene-Plugins-Official", DEV_ROOT / "Cyrene-Plugins-Official"),
        ("Cyrene-Client", DEV_ROOT / "Cyrene-Client"),
        ("Cyrene-Catalyst", DEV_ROOT / "Cyrene-Services" / "Cyrene-Catalyst"),
        ("Cyrene-Echo", DEV_ROOT / "Cyrene-Services" / "Cyrene-Echo"),
        ("Cyrene-Exchange", DEV_ROOT / "Cyrene-Services" / "Cyrene-Exchange"),
        ("Cyrene-Navigator", DEV_ROOT / "Cyrene-Services" / "Cyrene-Navigator"),
        ("Cyrene-Reactor", DEV_ROOT / "Cyrene-Services" / "Cyrene-Reactor"),
        ("Cyrene-Yield", DEV_ROOT / "Cyrene-Services" / "Cyrene-Yield"),
    ]

    for name, p in known_paths:
        if name not in repo_dirs and p.exists():
            repo_dirs[name] = p.resolve()

    targets: list[TargetRepo] = []
    for name, p in sorted(repo_dirs.items()):
        docs_dir = p / "docs"
        target_file = docs_dir / "logging-and-errors.md"
        targets.append(TargetRepo(name=name, docs_dir=docs_dir, target_file=target_file))

    return targets


def build_synced_content(canonical_content: str, is_canonical_file: bool = False) -> str:
    """Prepare content for target file.

    中文:为目标文件生成要同步的内容。
    """
    if is_canonical_file:
        return canonical_content
    return SYNC_HEADER + canonical_content


def sync_specs(*, check_only: bool = False, dry_run: bool = False) -> int:
    """Synchronize or verify the logging spec across all repositories.

    中文:同步日志规范,或验证各仓库中的副本是否一致。
    """
    if not CANONICAL_SPEC_FILE.exists():
        print(f"Error: Canonical spec not found at {CANONICAL_SPEC_FILE}", file=sys.stderr)
        return 1

    canonical_content = CANONICAL_SPEC_FILE.read_text(encoding="utf-8")
    targets = discover_repositories()
    drift_detected = False

    print(f"Canonical Source: {CANONICAL_SPEC_FILE}")
    print(f"Total Repositories Discovered: {len(targets)}\n")

    for target in targets:
        # Determine whether this is the canonical file itself
        # 中文:判断当前文件是否就是规范源文件本身
        # 中文:判断当前文件是否就是 canonical 文件。
        is_canonical = (target.target_file == CANONICAL_SPEC_FILE)
        expected_content = build_synced_content(canonical_content, is_canonical)

        if not target.docs_dir.exists():
            if not dry_run and not check_only:
                target.docs_dir.mkdir(parents=True, exist_ok=True)

        current_content = ""
        if target.target_file.exists():
            current_content = target.target_file.read_text(encoding="utf-8")

        if current_content == expected_content:
            status = "UP-TO-DATE"
            print(f"  [{status:12}] {target.name}: {target.target_file}")
        else:
            if check_only:
                status = "DRIFT / MISSING"
                drift_detected = True
                print(f"  [{status:12}] {target.name}: {target.target_file}")
            elif dry_run:
                status = "WOULD-UPDATE"
                print(f"  [{status:12}] {target.name}: {target.target_file}")
            else:
                target.target_file.write_text(expected_content, encoding="utf-8")
                status = "SYNCHRONIZED"
                print(f"  [{status:12}] {target.name}: {target.target_file}")

    if check_only and drift_detected:
        print("\nVerification FAILED: Out of sync documents detected.", file=sys.stderr)
        return 1

    print("\nSynchronization check / execution completed successfully.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synchronize Cyrene logging & error spec across all repository docs/ folders."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify that all repositories are synchronized without modifying them.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the synchronization process without modifying any files.",
    )
    args = parser.parse_args()

    exit_code = sync_specs(check_only=args.check, dry_run=args.dry_run)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
