"""Exercise catalog publication gates while immutable v1 history is retained.

模块职责：升级目录协议时验证历史发行物、精确资产和不可变发布门禁。
"""

from __future__ import annotations

import ast
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
        if isinstance(node, ast.FunctionDef)
        and node.name in {"catalog_asset_names", "check_release_metadata"}
    ]
    assert len(selected) == 2
    namespace: dict[str, object] = {}
    # Execute only the two reviewed functions from the versioned local workflow.
    exec(  # noqa: S102 - exercises the exact release gate without invoking its network code
        compile(ast.Module(body=selected, type_ignores=[]), str(WORKFLOW), "exec"), namespace
    )
    return namespace


def _release(version: int = 2) -> dict[str, object]:
    """Return published GitHub release metadata with the selected exact asset pair."""
    name = f"component-catalog-v{version}.json"
    return {
        "tag_name": "catalog-preview-" + "a" * 40,
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
