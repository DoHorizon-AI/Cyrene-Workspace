#!/usr/bin/env python3
"""
┌───────────────────────────────────────────────────────────────────────────────┐
│  📄 write_scope_guard.py                                                      │
│  Module: scripts.write_scope_guard                                            │
│  Role: Snapshot and verify repository write scope across the Cyrene workspace.│
│                                                                               │
│  模块职责：对 repositories.yaml 中的每个仓库记录 HEAD 与工作区状态快照；执行 verify │
│  时，任何未被 --allow 显式放行的仓库变化（含未跟踪文件与 HEAD 移动）即失败。        │
└───────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = Path("/tmp/cyrene-write-scope.json")


def _repositories() -> list[tuple[str, Path]]:
    """Read the canonical repository topology from repositories.yaml.

    中文：从 repositories.yaml 读取规范仓库拓扑。
    """

    manifest = WORKSPACE_ROOT / "repositories.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    repositories = data.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise SystemExit("repositories.yaml declares no repositories")
    entries: list[tuple[str, Path]] = []
    for entry in repositories:
        name = entry.get("name")
        path = entry.get("path")
        if not isinstance(name, str) or not isinstance(path, str):
            raise SystemExit("repositories.yaml entry is missing name or path")
        entries.append((name, (WORKSPACE_ROOT / path).resolve()))
    return entries


def _git(repo: Path, *args: str) -> str:
    """Run one read-only git query inside a repository.

    中文：在一个仓库中运行只读 git 查询。
    """

    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}"
        )
    return result.stdout


def _capture(repo: Path) -> dict:
    """Capture the observable state that defines a repository write scope.

    中文：采集定义仓库写入范围的可观察状态。
    """

    return {
        "head": _git(repo, "rev-parse", "HEAD").strip(),
        "status": sorted(
            line
            for line in _git(repo, "status", "--porcelain").splitlines()
            if line.strip()
        ),
    }


def _snapshot(snapshot_path: Path) -> int:
    """Write the current state of every repository to the snapshot file.

    中文：将每个仓库的当前状态写入 snapshot 文件。
    """

    state = {
        name: _capture(path)
        for name, path in _repositories()
        if path.is_dir()
    }
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"WRITE_SCOPE_SNAPSHOT: OK repos={len(state)} file={snapshot_path}")
    return 0


def _verify(snapshot_path: Path, allowed: set[str]) -> int:
    """Fail when a repository outside the allowed set changed since the snapshot.

    中文：如果允许集合之外的仓库在 snapshot 后发生变化，则失败。
    """

    if not snapshot_path.is_file():
        print(
            f"WRITE_SCOPE: FAIL: snapshot not found at {snapshot_path}; "
            "run with --snapshot first",
            file=sys.stderr,
        )
        return 2
    recorded = json.loads(snapshot_path.read_text(encoding="utf-8"))
    failures: list[str] = []
    allowed_changes: list[str] = []
    for name, path in _repositories():
        if name not in recorded:
            continue
        before = recorded[name]
        if not path.is_dir():
            failures.append(f"{name}: repository directory disappeared")
            continue
        current = _capture(path)
        if current == before:
            continue
        details: list[str] = []
        if current["head"] != before["head"]:
            details.append(
                f"HEAD moved {before['head'][:12]}... -> {current['head'][:12]}..."
            )
        before_status = set(before["status"])
        current_status = set(current["status"])
        added = sorted(current_status - before_status)
        removed = sorted(before_status - current_status)
        if added:
            details.append("new changes: " + ", ".join(added))
        if removed:
            details.append("cleared changes: " + ", ".join(removed))
        line = f"{name}: " + "; ".join(details)
        if name in allowed:
            allowed_changes.append(line)
        else:
            failures.append(line)

    for line in allowed_changes:
        print(f"WRITE_SCOPE_ALLOWED: {line}")
    if failures:
        print("WRITE_SCOPE: FAIL", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 2
    print(f"WRITE_SCOPE: PASS allowed={sorted(allowed)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Snapshot or verify repository write scope from the command line.

    中文：从命令行创建或验证 repository write-scope snapshot。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Record the current state of every repository.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Fail when a repository outside --allow changed since the snapshot.",
    )
    parser.add_argument(
        "--allow",
        default="",
        help="Comma-separated repository names whose changes are expected.",
    )
    parser.add_argument("--snapshot-file", type=Path, default=DEFAULT_SNAPSHOT)
    args = parser.parse_args(argv)
    if args.snapshot == args.verify:
        parser.error("choose exactly one of --snapshot or --verify")
    if args.snapshot:
        return _snapshot(args.snapshot_file)
    allowed = {name.strip() for name in args.allow.split(",") if name.strip()}
    return _verify(args.snapshot_file, allowed)


if __name__ == "__main__":
    raise SystemExit(main())
