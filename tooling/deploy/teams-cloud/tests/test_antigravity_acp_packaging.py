"""
┌──────────────────────────────────────────────────────────────────────────────┐
│  📄 test_antigravity_acp_packaging.py                                        │
│  Module: tooling.deploy.teams_cloud.tests.test_antigravity_acp_packaging    │
│  Role: Verify Google ACP pins, combined-image capability, and profile setup.│
│                                                                              │
│  模块职责：验证 Google ACP 固定版本、组合镜像能力和 profile 配置。         │
└──────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
build_arm64 = importlib.import_module("build_arm64")
preflight = importlib.import_module("preflight")


DEPLOYMENT_ROOT = Path(__file__).resolve().parents[1]


class _ComposeOverrideList(list[Any]):
    """Retain Compose's explicit list-replacement tag in the source test."""


class _ComposeLoader(yaml.SafeLoader):
    """Load the Compose tag used to drop the base CodeBuddy auth mount."""


_ComposeLoader.add_constructor(
    "!override",
    lambda loader, node: _ComposeOverrideList(loader.construct_sequence(node, deep=True)),
)


def _read_env_template(path: Path) -> dict[str, str]:
    """Read the selector-only key/value template without expanding host paths."""

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        assert separator
        values[name] = value
    return values


def test_antigravity_acp_env_template_matches_its_closed_schema() -> None:
    """Keep the owner profile, fixed command, authenticated model ID, and no-workflow selector."""

    template = _read_env_template(DEPLOYMENT_ROOT / "config/antigravity-acp.env.template")
    schema = json.loads(
        (DEPLOYMENT_ROOT / "config/antigravity-acp.env.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(template, schema)
    assert template["CYRENE_HARNESS_ANTIGRAVITY_ACP_MODEL"] == "gemini-3.8-flash"
    assert template["CYRENE_HARNESS_ANTIGRAVITY_ACP_COMMAND"] == (
        "/usr/local/bin/agy_acp_server.par"
    )
    assert template["CYRENE_HARNESS_ANTIGRAVITY_ACP_PROFILE"] == (
        "/home/navigator/.config/antigravity-acp"
    )
    assert template["CYRENE_HF_ENABLED"] == "false"
    assert template["CYRENE_WORKFLOWS_ENABLED"] == "false"


def test_antigravity_acp_compose_overlay_replaces_the_codebuddy_auth_mount() -> None:
    """Keep provider selection opt-in and Compose free of inherited CodeBuddy credentials."""

    compose_path = DEPLOYMENT_ROOT / "compose.antigravity-acp.yaml"
    overlay = yaml.load(compose_path.read_text(encoding="utf-8"), Loader=_ComposeLoader)
    assert set(overlay["services"]) == {"navigator"}
    navigator = overlay["services"]["navigator"]
    assert isinstance(navigator["volumes"], _ComposeOverrideList)
    assert navigator["environment"]["CYRENE_HARNESS_EXECUTION_BACKEND"] == "antigravity-acp"
    assert navigator["environment"]["CYRENE_HARNESS_ANTIGRAVITY_ACP_COMMAND"] == (
        "/usr/local/bin/agy_acp_server.par"
    )
    assert navigator["environment"]["CYRENE_HARNESS_ANTIGRAVITY_ACP_MODEL"] == (
        "${CYRENE_HARNESS_ANTIGRAVITY_ACP_MODEL:-}"
    )
    targets = {mount["target"] for mount in navigator["volumes"]}
    assert targets == {
        "/home/navigator/.config/antigravity-acp",
        "/var/lib/cyrene-navigator",
        "/run/teams-authorization",
    }
    assert not any("MCP" in name.upper() for name in navigator["environment"])


def test_antigravity_acp_dockerfile_uses_official_architecture_pins() -> None:
    """Require only the registry's immutable Google archives and hashes in the image recipe."""

    dockerfile = (DEPLOYMENT_ROOT / "Dockerfile.dsh-executor-antigravity-acp").read_text(
        encoding="utf-8"
    )
    expected_sources = {
        "https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-1.4.5-linux-x86_64.zip": (
            "923923a0d7dc8073f66cc6f78647a3107e3a7ee2b7e1a24df8b3927b5188013b"
        ),
        "https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-1.4.5-linux-arm64.zip": (
            "7d64246633ae094f67e3bbcd2d679e89048f91346d5339313336e7483f40f056"
        ),
    }
    for url, digest in expected_sources.items():
        assert url in dockerfile
        assert digest in dockerfile
    assert 'case "$TARGETARCH"' in dockerfile
    assert "sha256sum --check --status" in dockerfile
    assert 'unzip -p "$archive" agy_acp_server.par' in dockerfile
    assert 'unzip -p "$archive" localharness_external' in dockerfile
    assert "install -m 0555 /tmp/agy_acp_server.par /usr/local/bin/agy_acp_server.par" in dockerfile
    assert (
        "install -m 0555 /tmp/localharness_external /usr/local/bin/localharness_external"
        in dockerfile
    )
    assert "USER 10001:10001" in dockerfile
    assert "CODEBUDDY_PACKAGE" not in dockerfile
    for name, value in preflight.ANTIGRAVITY_ACP_IMAGE_LABELS.items():
        assert f'{name}="{value}"' in dockerfile


def test_codebuddy_executor_keeps_codebuddy_and_adds_the_pinned_antigravity_bundle() -> None:
    """Keep the normal CodeBuddy image active while packaging both pinned ACP executables."""

    dockerfile = (DEPLOYMENT_ROOT / "Dockerfile.dsh-executor-codebuddy").read_text(encoding="utf-8")
    expected_sources = {
        "https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-1.4.5-linux-x86_64.zip": (
            "923923a0d7dc8073f66cc6f78647a3107e3a7ee2b7e1a24df8b3927b5188013b"
        ),
        "https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-1.4.5-linux-arm64.zip": (
            "7d64246633ae094f67e3bbcd2d679e89048f91346d5339313336e7483f40f056"
        ),
    }
    for url, digest in expected_sources.items():
        assert url in dockerfile
        assert digest in dockerfile

    assert "FROM ${NODE_BASE_IMAGE} AS agy-acp-binary" in dockerfile
    assert 'case "$TARGETARCH"' in dockerfile
    assert "sha256sum --check --status" in dockerfile
    assert 'unzip -p "$archive" agy_acp_server.par' in dockerfile
    assert 'unzip -p "$archive" localharness_external' in dockerfile
    assert (
        "COPY --from=agy-acp-binary --chmod=0555 /usr/local/bin/agy_acp_server.par "
        "/usr/local/bin/agy_acp_server.par"
    ) in dockerfile
    assert (
        "COPY --from=agy-acp-binary --chmod=0555 /usr/local/bin/localharness_external "
        "/usr/local/bin/localharness_external"
    ) in dockerfile
    assert "ARG CODEBUDDY_PACKAGE=@tencent-ai/codebuddy-code@2.163.0" in dockerfile
    assert 'npm install --global "$CODEBUDDY_PACKAGE"' in dockerfile
    assert "codebuddy --version | grep -F '2.163.0'" in dockerfile
    assert "CODEBUDDY_CONFIG_DIR=/home/navigator/.codebuddy" in dockerfile
    assert "/home/navigator/.codebuddy" in dockerfile
    assert "CYRENE_HARNESS_ANTIGRAVITY_ACP_" not in dockerfile
    assert "/home/navigator/.config/antigravity-acp" not in dockerfile
    assert 'com.cyrene.navigator.runtime-profile="antigravity-acp"' not in dockerfile
    for name, value in preflight.ANTIGRAVITY_ACP_STANDBY_IMAGE_LABELS.items():
        assert f'{name}="{value}"' in dockerfile


def test_canonical_builder_accepts_a_two_image_executor_and_persistence_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a local candidate plan limited to the exact images without claiming a release."""

    template = json.loads(
        (DEPLOYMENT_ROOT / "config/build-plan.template.json").read_text(encoding="utf-8")
    )
    template["taskId"] = "vm-codebuddy-antigravity-dual-20261031"
    template["sources"]["workspace"]["commit"] = "a" * 40
    template["sources"]["navigator"]["commit"] = "b" * 40
    selected = [
        image
        for image in template["images"]
        if image.get("service") in {"dsh-executor", "navigator-persistence"}
    ]
    executor = next(image for image in selected if image["service"] == "dsh-executor")
    executor["dockerfile"] = (
        "Cyrene-Workspace/tooling/deploy/teams-cloud/Dockerfile.dsh-executor-codebuddy"
    )
    executor.pop("binaryArtifacts", None)
    plan = {
        "schemaVersion": template["schemaVersion"],
        "taskId": template["taskId"],
        "targetPlatform": "linux/arm64",
        "binaryArtifacts": [],
        "sources": {
            alias: template["sources"][alias] for alias in ("workspace", "navigator", "dsh")
        },
        "images": selected,
        "externalImages": {},
    }
    plan_path = tmp_path / "executor-persistence-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(build_arm64, "_private_artifact", lambda _path: True)

    validated = build_arm64.validate_plan(plan_path)

    assert [image["service"] for image in validated["images"]] == [
        "dsh-executor",
        "navigator-persistence",
    ]
    assert validated["images"][0]["dockerfile"].endswith("Dockerfile.dsh-executor-codebuddy")
    assert validated["images"][0]["buildArgs"]["CODEBUDDY_PACKAGE"] == (
        "@tencent-ai/codebuddy-code@2.163.0"
    )
    assert validated["binaryArtifacts"] == []


def test_antigravity_acp_build_plan_is_a_closed_single_image_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validate a reproducible opt-in image plan without invoking Docker or downloading assets."""

    template = json.loads(
        (DEPLOYMENT_ROOT / "config/build-plan.antigravity-acp.template.json").read_text(
            encoding="utf-8"
        )
    )
    for alias, commit in (("workspace", "a" * 40), ("navigator", "b" * 40)):
        template["sources"][alias]["commit"] = commit
    plan_path = tmp_path / "antigravity-acp-plan.json"
    plan_path.write_text(json.dumps(template), encoding="utf-8")
    monkeypatch.setattr(build_arm64, "_private_artifact", lambda _path: True)
    plan = build_arm64.validate_plan(plan_path)

    assert plan["targetPlatform"] == "linux/arm64"
    assert set(plan["sources"]) == {"workspace", "navigator", "dsh"}
    assert [image["service"] for image in plan["images"]] == ["dsh-executor"]
    assert plan["images"][0]["dockerfile"].endswith("Dockerfile.dsh-executor-antigravity-acp")
    assert "CODEBUDDY_PACKAGE" not in plan["images"][0]["buildArgs"]
