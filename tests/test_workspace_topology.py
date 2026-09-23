"""Regression tests for the canonical multi-repository topology."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "repositories.yaml"


def _topology() -> dict[str, object]:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def test_full_profile_contains_every_canonical_repository_once() -> None:
    topology = _topology()
    repositories = topology["repositories"]
    names = [repository["name"] for repository in repositories]
    full_profile = topology["profiles"]["full"]["repositories"]

    assert len(names) == len(set(names))
    assert len(full_profile) == len(set(full_profile))
    assert set(full_profile) == set(names)


def test_clone_policies_match_declared_visibility() -> None:
    repositories = _topology()["repositories"]

    for repository in repositories:
        if repository["visibility"] == "public":
            assert repository["clone_policy"] == "public_zero_auth"
        else:
            assert repository["clone_policy"] != "public_zero_auth"

    studio = next(
        repository for repository in repositories if repository["name"] == "Cyrene-Studio"
    )
    assert studio["visibility"] == "private"
    assert studio["clone_policy"] == "github_auth_required"


def test_workspace_entrypoints_cover_the_full_profile() -> None:
    topology = _topology()
    canonical_names = set(topology["profiles"]["full"]["repositories"])

    bootstrap = (ROOT / "bootstrap.ps1").read_text(encoding="utf-8")
    bootstrap_names = set(re.findall(r'Name\s*=\s*"(Cyrene-[^"]+)"', bootstrap))
    assert canonical_names <= bootstrap_names

    idea_root = ET.parse(ROOT / ".idea" / "jb-workspace.xml").getroot()
    mounted_names = {project.attrib["name"] for project in idea_root.findall(".//project")}
    assert canonical_names <= mounted_names

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in canonical_names:
        assert f"`{name}`" in readme

    worktree_helper = (ROOT / "agent-worktree.ps1").read_text(encoding="utf-8")
    assert re.search(r'"studio"\s*=\s*"Cyrene-Studio"', worktree_helper)
