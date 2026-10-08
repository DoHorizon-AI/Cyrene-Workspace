"""Exercise catalog publication gates while immutable v1 history is retained.

模块职责：升级目录协议时验证历史发行物、精确资产和不可变发布门禁。
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/component-catalog-release.yml"


def _history_gate() -> dict[str, object]:
    """Load the exact publication gate functions without running network operations."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index('          repository = os.environ["REPOSITORY"]')
    end = workflow.index("          PY\n", start)
    parsed = ast.parse(textwrap.dedent(workflow[start:end]))
    selected = [
        node
        for node in parsed.body
        if (
            isinstance(node, ast.FunctionDef)
            and node.name in {"catalog_asset_names", "check_release_metadata"}
        )
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "tag_pattern"
                for target in node.targets
            )
        )
    ]
    assert len(selected) == 3
    namespace: dict[str, object] = {"re": re}
    # Execute only the two reviewed functions from the versioned local workflow.
    exec(  # noqa: S102 - exercises the exact release gate without invoking its network code
        compile(ast.Module(body=selected, type_ignores=[]), str(WORKFLOW), "exec"), namespace
    )
    return namespace


def _release(version: int = 2) -> dict[str, object]:
    """Return published GitHub release metadata with the selected exact asset pair."""
    name = f"component-catalog-v{version}.json"
    return {
        "tag_name": ("catalog-v2-preview-" if version == 2 else "catalog-preview-") + "a" * 40,
        "draft": False,
        "immutable": True,
        "prerelease": True,
        "assets": [
            {"name": name, "state": "uploaded"},
            {"name": name + ".attestation.jsonl", "state": "uploaded"},
        ],
    }


@pytest.mark.parametrize("version", [1, 2])
def test_existing_signed_catalog_versions_are_admitted_for_history(version: int) -> None:
    """The v2 publisher must still verify an immutable v1 release's own asset names."""
    gate = _history_gate()["check_release_metadata"]
    release = _release(version)
    assert gate(release, release["tag_name"], "preview") == (
        f"component-catalog-v{version}.json",
        f"component-catalog-v{version}.json.attestation.jsonl",
    )


@pytest.mark.parametrize("mutation", ["mixed", "duplicate", "extra", "missing", "future"])
def test_ambiguous_or_unsupported_history_assets_are_rejected(mutation: str) -> None:
    """A migration may not accept a mixed pair, unverified extra asset or new protocol."""
    release = _release()
    if mutation == "mixed":
        release["assets"][1]["name"] = "component-catalog-v1.json.attestation.jsonl"
    elif mutation == "duplicate":
        release["assets"].append(dict(release["assets"][0]))
    elif mutation == "extra":
        release["assets"].append({"name": "unverified.zip", "state": "uploaded"})
    elif mutation == "missing":
        release["assets"].pop()
    else:
        release = _release(3)
    with pytest.raises(RuntimeError):
        _history_gate()["check_release_metadata"](release, release["tag_name"], "preview")


@pytest.mark.parametrize(
    ("field", "value"), [("draft", True), ("immutable", False), ("prerelease", False)]
)
def test_v1_migration_preserves_publication_identity_gates(field: str, value: bool) -> None:
    """Accepting historical names must not relax immutable publication and channel checks."""
    release = _release(1)
    release[field] = value
    with pytest.raises(RuntimeError):
        _history_gate()["check_release_metadata"](release, release["tag_name"], "preview")


def test_history_rejects_incomplete_uploads_and_wrong_tag() -> None:
    """Byte verification only follows complete assets and an exact source tag match."""
    gate = _history_gate()["check_release_metadata"]
    release = _release(1)
    release["assets"][1]["state"] = "new"
    with pytest.raises(RuntimeError, match="not uploaded"):
        gate(release, release["tag_name"], "preview")
    with pytest.raises(RuntimeError, match="wrong tag"):
        gate(_release(), "catalog-preview-" + "b" * 40, "preview")


@pytest.mark.parametrize("version", [1, 2])
def test_catalog_tag_cannot_claim_another_schema_version(version: int) -> None:
    """Separate immutable namespaces protect publishers still pinned to v1 discovery."""
    release = _release(version)
    other_version = 1 if version == 2 else 2
    release["tag_name"] = _release(other_version)["tag_name"]
    with pytest.raises(RuntimeError, match="tag and schema asset version"):
        _history_gate()["check_release_metadata"](release, release["tag_name"], "preview")


@pytest.mark.parametrize("tag", ["catalog-v3-preview-" + "a" * 40, "catalog-v2-preview-deadbeef"])
def test_history_rejects_unknown_or_partial_source_tags(tag: str) -> None:
    """Only exact source SHA tags in the two published namespaces are accepted."""
    release = _release()
    release["tag_name"] = tag
    with pytest.raises(RuntimeError, match="invalid source tag"):
        _history_gate()["check_release_metadata"](release, tag, "preview")


def test_source_tag_must_match_the_asserted_channel() -> None:
    """A stable prerelease flag cannot disguise a preview source namespace."""
    release = _release()
    release["prerelease"] = False
    with pytest.raises(RuntimeError, match="invalid source tag or channel"):
        _history_gate()["check_release_metadata"](release, release["tag_name"], "stable")
