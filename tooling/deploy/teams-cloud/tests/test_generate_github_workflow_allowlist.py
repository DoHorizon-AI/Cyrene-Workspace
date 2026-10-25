"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_generate_github_workflow_allowlist.py                           │
│  Module: tooling.deploy.teams_cloud.tests.test_github_workflow_allowlist │
│  Role: Verify one inventory generates matching private rerun scopes.     │
│                                                                          │
│  模块职责：验证同一核实清单生成一致的私有 CI 重跑授权。                   │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import copy
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
generator = importlib.import_module("generate_github_workflow_allowlist")
preflight = importlib.import_module("preflight")

WORKSPACE = "DoHorizon-AI/Cyrene-Workspace"
SOURCE_REPOSITORIES = [
    "Cyrene-Platform",
    "Cyrene-Plugins-Official",
    "Cyrene-Reactor",
    "Cyrene-Yield",
    "Cyrene-Exchange",
    "Cyrene-Catalyst",
    "Cyrene-Echo",
    "Cyrene-Navigator",
    "Cyrene-Client",
]


def _write_private_json(path: Path, document: dict[str, Any]) -> None:
    """Write one test input with the same private permissions as deployment artifacts."""

    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _task_artifact(tmp_path: Path) -> Path:
    """Create one owner-only ten-repository target/workflow input fixture."""

    artifact = tmp_path / ".artifacts" / "workflow-allowlist-test"
    config = artifact / "config"
    config.mkdir(parents=True)
    config.chmod(0o700)
    artifact.chmod(0o700)

    inventory_rows = {
        f"DoHorizon-AI/{name}": {
            "id": str(900_000 + index),
            "name": "public-ci" if name == "Cyrene-Plugins-Official" else "ci",
            "path": ".github/workflows/public-ci.yml"
            if name == "Cyrene-Plugins-Official"
            else ".github/workflows/ci.yml",
        }
        for index, name in enumerate(SOURCE_REPOSITORIES, start=1)
    }
    inventory = {
        "schemaVersion": 1,
        "observedAt": "2026-10-25T00:00:00Z",
        "source": generator.WORKFLOW_SOURCE,
        "repositories": inventory_rows,
        "excluded": {
            WORKSPACE: {"reason": "CI workflow also creates immutable release artifacts."}
        },
    }
    _write_private_json(artifact / generator.WORKFLOW_INVENTORY_NAME, inventory)

    labels = [WORKSPACE, *inventory_rows]
    target_rows = []
    for index, label in enumerate(labels, start=1):
        target_id = str(100_000 + index)
        grants = [
            {
                "id": "draft_pull_request",
                "action": "draft_pull_request",
                "baseBranches": ["develop"],
                "branchPrefixes": ["cyrene/assistant/"],
                "pathPrefixes": ["**"],
                "issueNumbers": [],
                "sourceWorkflowIds": [],
                "permittedOperations": [
                    "github.branch.create",
                    "github.file.commit",
                    "github.pull_request.draft.create",
                ],
            }
        ]
        if label != WORKSPACE:
            grants.append(
                {
                    "id": "rerun_source_ci",
                    "action": "rerun_source_ci",
                    "baseBranches": [],
                    "branchPrefixes": ["cyrene/assistant/"],
                    "pathPrefixes": [],
                    "issueNumbers": [],
                    "sourceWorkflowIds": [],
                    "permittedOperations": ["github.actions.rerun_failed"],
                }
            )
        target_rows.append(
            {
                "id": target_id,
                "label": label,
                "bindingId": "UNBOUND_PENDING_EXCHANGE_GITHUB_REBIND",
                "uri": f"https://github.com/{label}",
                "actionGrants": grants,
            }
        )
    targets = {"schemaVersion": 1, "targets": target_rows}
    _write_private_json(config / generator.TARGETS_NAME, targets)

    target_ids_by_label = {row["label"]: row["id"] for row in target_rows}
    target_ids = [target_ids_by_label[label] for label in labels]
    source_target_ids = [target_ids_by_label[label] for label in inventory_rows]
    workflow = {
        "version": 2,
        "id": "cyrene-repository-responsibility-v1",
        "targets": [
            {
                "kind": "github_repository",
                "id": target_ids_by_label[label],
                "label": label,
                "uri": f"https://github.com/{label}",
                "bindingId": "UNBOUND_PENDING_EXCHANGE_GITHUB_REBIND",
            }
            for label in labels
        ],
        "actionPolicy": {
            "allowedActions": [
                {
                    "id": "draft_pull_request",
                    "action": "draft_pull_request",
                    "targetIds": target_ids,
                    "baseBranches": ["develop"],
                    "branchPrefixes": ["cyrene/assistant/"],
                    "pathPrefixes": ["**"],
                    "issueNumbers": [],
                    "sourceWorkflowIds": [],
                    "permittedOperations": [
                        "github.branch.create",
                        "github.file.commit",
                        "github.pull_request.draft.create",
                    ],
                },
                {
                    "id": "rerun_source_ci",
                    "action": "rerun_source_ci",
                    "targetIds": source_target_ids,
                    "baseBranches": [],
                    "branchPrefixes": ["cyrene/assistant/"],
                    "pathPrefixes": [],
                    "issueNumbers": [],
                    "sourceWorkflowIds": [],
                    "permittedOperations": ["github.actions.rerun_failed"],
                },
            ]
        },
        "enabled": False,
    }
    _write_private_json(artifact / generator.WORKFLOW_NAME, workflow)
    return artifact


def test_generator_builds_matching_provider_and_navigator_allowlists(tmp_path: Path) -> None:
    """Generate the provider map and both Navigator grant layers from one inventory."""

    artifact = _task_artifact(tmp_path)
    result = generator.generate_workflow_allowlist(artifact)
    config_dir = artifact / "config"
    provider = json.loads((config_dir / generator.PROVIDER_FRAGMENT_NAME).read_text())
    targets = json.loads((config_dir / generator.TARGETS_NAME).read_text())
    workflow = json.loads((artifact / generator.WORKFLOW_NAME).read_text())
    source_ids = {str(100_000 + index + 1): [str(900_000 + index)] for index in range(1, 10)}

    assert provider == {"source_workflow_ids": source_ids}
    for target in targets["targets"]:
        reruns = [grant for grant in target["actionGrants"] if grant["id"] == "rerun_source_ci"]
        if target["label"] == WORKSPACE:
            assert reruns == []
        else:
            repository_number = int(target["id"]) - 100_000 - 1
            assert len(reruns) == 1
            assert reruns[0]["sourceWorkflowIds"] == [str(900_000 + repository_number)]
    rerun = next(
        row for row in workflow["actionPolicy"]["allowedActions"] if row["id"] == "rerun_source_ci"
    )
    assert WORKSPACE not in {
        row["label"] for row in workflow["targets"] if row["id"] in rerun["targetIds"]
    }
    assert rerun["sourceWorkflowIds"] == sorted(
        workflow_id for values in source_ids.values() for workflow_id in values
    )
    assert result["repositoryCount"] == 10
    assert result["sourceWorkflowRepositoryCount"] == 9
    for path in (
        config_dir / generator.PROVIDER_FRAGMENT_NAME,
        config_dir / generator.TARGETS_NAME,
        artifact / generator.WORKFLOW_NAME,
    ):
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_repo",
        "extra_repo",
        "duplicate_target_id",
        "missing_workflow_id",
        "extra_workflow_id",
        "duplicate_workflow_id",
    ],
)
def test_generator_fails_closed_on_unverified_inventory_sets(tmp_path: Path, mutation: str) -> None:
    """Reject missing, extra, or duplicate source rows before writing outputs."""

    artifact = _task_artifact(tmp_path)
    inventory_path = artifact / generator.WORKFLOW_INVENTORY_NAME
    inventory = json.loads(inventory_path.read_text())
    if mutation == "missing_repo":
        inventory["repositories"].pop(next(iter(inventory["repositories"])))
    elif mutation == "extra_repo":
        inventory["repositories"]["DoHorizon-AI/Cyrene-Extra"] = {
            "id": "999999",
            "name": "ci",
            "path": ".github/workflows/ci.yml",
        }
    elif mutation == "duplicate_target_id":
        targets_path = artifact / "config" / generator.TARGETS_NAME
        targets = json.loads(targets_path.read_text())
        targets["targets"][1]["id"] = targets["targets"][0]["id"]
        _write_private_json(targets_path, targets)
    elif mutation == "missing_workflow_id":
        del next(iter(inventory["repositories"].values()))["id"]
    elif mutation == "extra_workflow_id":
        next(iter(inventory["repositories"].values()))["id"] += ",999999"
    else:
        rows = list(inventory["repositories"].values())
        rows[1]["id"] = rows[0]["id"]
    _write_private_json(inventory_path, inventory)

    with pytest.raises(generator.AllowlistGenerationError):
        generator.generate_workflow_allowlist(artifact)
    assert not (artifact / "config" / generator.PROVIDER_FRAGMENT_NAME).exists()


@pytest.mark.parametrize("output_name", ["provider_fragment", "navigator_target"])
def test_generator_does_not_overwrite_extra_existing_workflow_ids(
    tmp_path: Path, output_name: str
) -> None:
    """Require a human to reconcile stale or extra IDs instead of silently replacing them."""

    artifact = _task_artifact(tmp_path)
    generator.generate_workflow_allowlist(artifact)
    config_dir = artifact / "config"
    if output_name == "provider_fragment":
        path = config_dir / generator.PROVIDER_FRAGMENT_NAME
        document = json.loads(path.read_text())
        first_repo = next(iter(document["source_workflow_ids"]))
        document["source_workflow_ids"][first_repo].append("999999999")
    else:
        path = config_dir / generator.TARGETS_NAME
        document = json.loads(path.read_text())
        target = next(row for row in document["targets"] if row["label"] != WORKSPACE)
        grant = next(row for row in target["actionGrants"] if row["id"] == "rerun_source_ci")
        grant["sourceWorkflowIds"].append("999999999")
    _write_private_json(path, document)

    with pytest.raises(generator.AllowlistGenerationError, match="extra|mismatched"):
        generator.generate_workflow_allowlist(artifact)


def _preflight_config(artifact: Path) -> dict[str, Any]:
    """Create a Compose projection for one generated provider profile."""

    generator.generate_workflow_allowlist(artifact)
    config_dir = artifact / "config"
    targets = json.loads((config_dir / generator.TARGETS_NAME).read_text())
    provider_fragment = json.loads((config_dir / generator.PROVIDER_FRAGMENT_NAME).read_text())
    provider_configuration = {
        "repositories": [
            {
                "repository_id": target["id"],
                "owner": target["label"].split("/", 1)[0],
                "name": target["label"].split("/", 1)[1],
            }
            for target in targets["targets"]
        ],
        **provider_fragment,
    }
    return {
        "services": {
            "github-issues": {
                "environment": {
                    "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(provider_configuration)
                }
            }
        }
    }


def test_preflight_requires_matching_provider_and_navigator_workflow_ids(
    tmp_path: Path,
) -> None:
    """Reject missing/extra workflow IDs in the provider map or Navigator grants."""

    artifact = _task_artifact(tmp_path)
    config = _preflight_config(artifact)
    config_dir = artifact / "config"
    preflight._validate_github_workflow_allowlist(config, config_dir)

    provider_env_key = "CYRENE_CAPABILITY_CONFIGURATION_JSON"
    invalid_configs = []
    for mutate in (
        lambda mapping: mapping[next(iter(mapping))].clear(),
        lambda mapping: mapping[next(iter(mapping))].append("999999999"),
        lambda mapping: mapping.update({"999999": ["999999999"]}),
    ):
        invalid = copy.deepcopy(config)
        provider_configuration = json.loads(
            invalid["services"]["github-issues"]["environment"][provider_env_key]
        )
        mutate(provider_configuration["source_workflow_ids"])
        invalid["services"]["github-issues"]["environment"][provider_env_key] = json.dumps(
            provider_configuration
        )
        invalid_configs.append(invalid)

    missing_repository = copy.deepcopy(config)
    provider = json.loads(
        missing_repository["services"]["github-issues"]["environment"][provider_env_key]
    )
    provider["repositories"].pop()
    missing_repository["services"]["github-issues"]["environment"][provider_env_key] = json.dumps(
        provider
    )
    invalid_configs.append(missing_repository)

    extra_repository = copy.deepcopy(config)
    provider = json.loads(
        extra_repository["services"]["github-issues"]["environment"][provider_env_key]
    )
    provider["repositories"].append(
        {"repository_id": "999999", "owner": "DoHorizon-AI", "name": "Cyrene-Extra"}
    )
    extra_repository["services"]["github-issues"]["environment"][provider_env_key] = json.dumps(
        provider
    )
    invalid_configs.append(extra_repository)
    for invalid in invalid_configs:
        with pytest.raises(preflight.PreflightError):
            preflight._validate_github_workflow_allowlist(invalid, config_dir)

    targets_path = config_dir / generator.TARGETS_NAME
    targets = json.loads(targets_path.read_text())
    valid_targets = copy.deepcopy(targets)
    target = next(row for row in targets["targets"] if row["label"] != WORKSPACE)
    grant = next(row for row in target["actionGrants"] if row["id"] == "rerun_source_ci")
    grant["sourceWorkflowIds"] = ["999999999"]
    _write_private_json(targets_path, targets)
    with pytest.raises(preflight.PreflightError, match="per-repository rerun grant"):
        preflight._validate_github_workflow_allowlist(config, config_dir)

    _write_private_json(targets_path, valid_targets)
    workflow_path = artifact / generator.WORKFLOW_NAME
    workflow = json.loads(workflow_path.read_text())
    action = next(
        row for row in workflow["actionPolicy"]["allowedActions"] if row["id"] == "rerun_source_ci"
    )
    action["sourceWorkflowIds"] = ["999999999"]
    _write_private_json(workflow_path, workflow)
    with pytest.raises(preflight.PreflightError, match="WorkFlow rerun grant"):
        preflight._validate_github_workflow_allowlist(config, config_dir)
