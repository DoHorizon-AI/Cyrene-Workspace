"""Materialize and verify the pinned lifecycle Product revisions. | 拉取并校验生命周期锁定版本。

The lifecycle harness resolves every Product from an exact checkout, so CI and
local acceptance runs share one implementation for reading ``sources.json``,
fetching each pinned revision, and exporting the ``CYRENE_*_WORKTREE``
environment the cross-repository tests require.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

WORKSPACE = Path(__file__).resolve().parents[1]
SOURCES = WORKSPACE / "ci" / "text-lifecycle-v1" / "sources.json"
REPOSITORIES = WORKSPACE / "repositories.yaml"
PLATFORM_REPOSITORY = "Cyrene-Platform"


def _git(arguments: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _remotes() -> dict[str, str]:
    topology = yaml.safe_load(REPOSITORIES.read_text(encoding="utf-8"))
    return {
        entry["name"]: entry["canonical_remote"]
        for entry in topology["repositories"]
        if entry.get("canonical_remote")
    }


def _pins() -> list[tuple[str, str]]:
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))
    pins = [(f"Cyrene-{product}", revision) for product, revision in sources["products"].items()]
    pins.append((PLATFORM_REPOSITORY, sources["platformRevision"]))
    return pins


def _checkout(remote: str, revision: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if not (destination / ".git").is_dir():
        _git(["init", "--quiet"], destination)
        _git(["remote", "add", "origin", remote], destination)
    _git(["fetch", "--depth", "1", "origin", revision], destination)
    _git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], destination)
    _git(["cat-file", "-e", revision + "^{commit}"], destination)


def _environment_name(repository: str) -> str:
    return "CYRENE_" + repository.removeprefix("Cyrene-").upper().replace("-", "_") + "_WORKTREE"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path(os.environ.get("CYRENE_CHECKOUT_ROOT", WORKSPACE.parent / "sources")),
        help="Directory holding one checkout per pinned repository",
    )
    parser.add_argument(
        "--print-env",
        action="store_true",
        help="Print the CYRENE_*_WORKTREE exports the lifecycle tests consume",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only confirm every already-materialized checkout contains its revision",
    )
    arguments = parser.parse_args()

    remotes = _remotes()
    failures: list[str] = []
    for repository, revision in _pins():
        destination = arguments.destination / repository
        remote = remotes.get(repository)
        if remote is None:
            failures.append(f"{repository}: no canonical remote is declared")
            continue
        if arguments.verify_only:
            if not (destination / ".git").is_dir():
                failures.append(f"{repository}: checkout is missing at {destination}")
                continue
            try:
                _git(["cat-file", "-e", revision + "^{commit}"], destination)
            except subprocess.CalledProcessError:
                failures.append(f"{repository}: {revision} is not present in {destination}")
                continue
        else:
            try:
                _checkout(remote, revision, destination)
            except subprocess.CalledProcessError as exc:
                failures.append(f"{repository}: cannot fetch {revision}: {exc.stderr.strip()}")
                continue
        if arguments.print_env and repository != PLATFORM_REPOSITORY:
            print(f"{_environment_name(repository)}={destination.resolve()}")

    if failures:
        for failure in failures:
            print("FAIL " + failure, file=sys.stderr)
        return 1
    print(f"Verified {len(_pins())} pinned revisions under {arguments.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
