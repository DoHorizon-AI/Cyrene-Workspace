"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 generate_github_workflow_allowlist.py                                │
│  Module: tooling.deploy.teams_cloud.generate_github_workflow_allowlist   │
│  Role: Generate matching private provider and Navigator CI rerun scopes.  │
│                                                                          │
│  模块职责：从已核实清单生成一致的 Provider 与 Navigator CI 重跑范围。     │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

WORKSPACE_REPOSITORY = "DoHorizon-AI/Cyrene-Workspace"
WORKFLOW_INVENTORY_NAME = "github-source-workflows.json"
TARGETS_NAME = "responsibility-targets.v1.json"
WORKFLOW_NAME = "proactive-assistant-workflow.v2.json"
PROVIDER_FRAGMENT_NAME = "github-issues-source-workflow-ids.v1.json"
WORKFLOW_SOURCE = "GitHub REST GET /repos/{owner}/{repo}/actions/workflows?per_page=100"
SOURCE_WORKFLOW_PATHS = {
    ".github/workflows/ci.yml",
    ".github/workflows/public-ci.yml",
}
WORKFLOW_ID_PATTERN = re.compile(r"[1-9][0-9]{0,19}")
REPOSITORY_LABEL_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
REPOSITORY_ID_PATTERN = re.compile(r"[1-9][0-9]*")


class AllowlistGenerationError(ValueError):
    """The verified inventory and host-owned responsibility grants do not agree."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys so the allowlist has one interpretation."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AllowlistGenerationError("allowlist input contains a duplicate JSON key")
        result[key] = value
    return result


def _read_private_json(path: Path, artifact_root: Path, label: str) -> dict[str, Any]:
    """Load one owner-only JSON file under the selected private task artifact directory."""

    resolved_path = path.resolve()
    resolved_root = artifact_root.resolve()
    if path.is_symlink() or not path.is_file() or not resolved_path.is_relative_to(resolved_root):
        raise AllowlistGenerationError(
            f"{label} must be a regular file under the task artifact directory"
        )
    if path.stat().st_mode & 0o077:
        raise AllowlistGenerationError(f"{label} must be owner-only")
    try:
        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AllowlistGenerationError(f"{label} is not valid JSON") from error
    if not isinstance(document, dict):
        raise AllowlistGenerationError(f"{label} must be a JSON object")
    return document


def _validate_artifact_directory(path: Path) -> Path:
    """Require an existing private directory under the umbrella artifact root."""

    if path.is_symlink() or not path.is_dir():
        raise AllowlistGenerationError(
            "task artifact directory must already exist and not be a symlink"
        )
    resolved = path.resolve()
    if ".artifacts" not in resolved.parts or any(
        (parent / ".git").exists() for parent in (resolved, *resolved.parents)
    ):
        raise AllowlistGenerationError(
            "allowlist inputs and outputs must stay outside Git worktrees"
        )
    if resolved.stat().st_mode & 0o077:
        raise AllowlistGenerationError("task artifact directory must be owner-only")
    config_dir = resolved / "config"
    if config_dir.is_symlink() or not config_dir.is_dir() or config_dir.stat().st_mode & 0o077:
        raise AllowlistGenerationError(
            "private config directory must already exist and be owner-only"
        )
    return resolved


def _validate_inventory(document: dict[str, Any]) -> dict[str, list[str]]:
    """Validate the closed nine-repository source-CI inventory and excluded Workspace entry."""

    if (
        set(document) != {"schemaVersion", "observedAt", "source", "repositories", "excluded"}
        or document.get("schemaVersion") != 1
        or isinstance(document.get("schemaVersion"), bool)
        or not isinstance(document.get("observedAt"), str)
        or not document["observedAt"]
        or document.get("source") != WORKFLOW_SOURCE
    ):
        raise AllowlistGenerationError("workflow inventory has an unsupported schema or source")
    repositories = document.get("repositories")
    excluded = document.get("excluded")
    if (
        not isinstance(repositories, dict)
        or len(repositories) != 9
        or not isinstance(excluded, dict)
        or set(excluded) != {WORKSPACE_REPOSITORY}
        or not isinstance(excluded[WORKSPACE_REPOSITORY], dict)
        or not isinstance(excluded[WORKSPACE_REPOSITORY].get("reason"), str)
    ):
        raise AllowlistGenerationError(
            "workflow inventory must contain exactly nine source repos and exclude Workspace"
        )
    ids_by_repository: dict[str, list[str]] = {}
    all_workflow_ids: set[str] = set()
    for label, row in repositories.items():
        if (
            not isinstance(label, str)
            or not REPOSITORY_LABEL_PATTERN.fullmatch(label)
            or label == WORKSPACE_REPOSITORY
            or not isinstance(row, dict)
            or set(row) != {"id", "name", "path"}
        ):
            raise AllowlistGenerationError("workflow inventory has an invalid source repository")
        workflow_id = row.get("id")
        if (
            not isinstance(workflow_id, str)
            or not WORKFLOW_ID_PATTERN.fullmatch(workflow_id)
            or workflow_id in all_workflow_ids
            or not isinstance(row.get("name"), str)
            or not row["name"]
            or row.get("path") not in SOURCE_WORKFLOW_PATHS
        ):
            raise AllowlistGenerationError(
                "workflow inventory has an invalid, duplicate, or non-source workflow ID"
            )
        all_workflow_ids.add(workflow_id)
        ids_by_repository[label] = [workflow_id]
    return ids_by_repository


def _validate_targets(
    document: dict[str, Any], source_labels: set[str]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Require exactly the ten approved targets and preserve their existing bindings."""

    targets = document.get("targets")
    expected_labels = source_labels | {WORKSPACE_REPOSITORY}
    if (
        set(document) != {"schemaVersion", "targets"}
        or document.get("schemaVersion") != 1
        or isinstance(document.get("schemaVersion"), bool)
        or not isinstance(targets, list)
        or len(targets) != 10
    ):
        raise AllowlistGenerationError(
            "responsibility targets must contain exactly ten repositories"
        )
    by_label: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {
            "id",
            "label",
            "bindingId",
            "uri",
            "actionGrants",
        }:
            raise AllowlistGenerationError("responsibility target does not match the closed schema")
        repository_id = target.get("id")
        label = target.get("label")
        if (
            not isinstance(repository_id, str)
            or not REPOSITORY_ID_PATTERN.fullmatch(repository_id)
            or repository_id in seen_ids
            or not isinstance(label, str)
            or label in by_label
            or target.get("uri") != f"https://github.com/{label}"
            or not isinstance(target.get("bindingId"), str)
            or not target["bindingId"]
            or not isinstance(target.get("actionGrants"), list)
        ):
            raise AllowlistGenerationError(
                "responsibility target identity is missing or duplicated"
            )
        seen_ids.add(repository_id)
        by_label[label] = target
    if set(by_label) != expected_labels:
        raise AllowlistGenerationError(
            "responsibility target repos do not match the verified inventory"
        )

    for label, target in by_label.items():
        rerun_grants = [
            grant
            for grant in target["actionGrants"]
            if isinstance(grant, dict) and grant.get("id") == "rerun_source_ci"
        ]
        expected_count = 0 if label == WORKSPACE_REPOSITORY else 1
        if len(rerun_grants) != expected_count:
            raise AllowlistGenerationError(
                "source repos need one rerun grant and Workspace must remain excluded"
            )
        if rerun_grants:
            grant = rerun_grants[0]
            if (
                grant.get("action") != "rerun_source_ci"
                or grant.get("branchPrefixes") != ["cyrene/assistant/"]
                or grant.get("permittedOperations") != ["github.actions.rerun_failed"]
            ):
                raise AllowlistGenerationError(
                    "rerun grant exceeds the approved task-branch operation"
                )
    return targets, by_label


def _validate_workflow_document(
    document: dict[str, Any],
    target_by_label: dict[str, dict[str, Any]],
    source_workflows: dict[str, list[str]],
) -> dict[str, Any]:
    """Validate the disabled v2 workflow before replacing its repository workflow IDs."""

    targets = document.get("targets")
    policy = document.get("actionPolicy")
    allowed_actions = policy.get("allowedActions") if isinstance(policy, dict) else None
    if (
        document.get("version") != 2
        or not isinstance(targets, list)
        or not isinstance(allowed_actions, list)
    ):
        raise AllowlistGenerationError("Navigator workflow must use the v2 target/action structure")
    target_by_id: dict[str, dict[str, Any]] = {}
    for target in targets:
        if not isinstance(target, dict) or target.get("kind") != "github_repository":
            raise AllowlistGenerationError("workflow target is not a GitHub repository")
        repository_id = target.get("id")
        label = target.get("label")
        expected = target_by_label.get(label) if isinstance(label, str) else None
        if (
            not isinstance(repository_id, str)
            or expected is None
            or repository_id != expected.get("id")
            or target.get("uri") != expected.get("uri")
            or target.get("bindingId") != expected.get("bindingId")
            or repository_id in target_by_id
        ):
            raise AllowlistGenerationError(
                "workflow targets do not match the host target configuration"
            )
        target_by_id[repository_id] = target
    expected_ids = {target["id"] for target in target_by_label.values()}
    if set(target_by_id) != expected_ids:
        raise AllowlistGenerationError("workflow must target the same exact ten repositories")
    rerun_actions = [
        action
        for action in allowed_actions
        if isinstance(action, dict) and action.get("id") == "rerun_source_ci"
    ]
    if len(rerun_actions) != 1:
        raise AllowlistGenerationError("workflow must have exactly one source-CI rerun action")
    rerun_action = rerun_actions[0]
    source_target_ids = {target_by_label[label]["id"] for label in source_workflows}
    workspace_id = target_by_label[WORKSPACE_REPOSITORY]["id"]
    existing_target_ids = rerun_action.get("targetIds")
    expected_workflow_ids = sorted(
        workflow_id for workflow_ids in source_workflows.values() for workflow_id in workflow_ids
    )
    existing_workflow_ids = rerun_action.get("sourceWorkflowIds")
    if (
        rerun_action.get("action") != "rerun_source_ci"
        or rerun_action.get("branchPrefixes") != ["cyrene/assistant/"]
        or rerun_action.get("baseBranches") != []
        or rerun_action.get("pathPrefixes") != []
        or rerun_action.get("issueNumbers") != []
        or rerun_action.get("permittedOperations") != ["github.actions.rerun_failed"]
        or not isinstance(existing_target_ids, list)
        or any(not isinstance(item, str) for item in existing_target_ids)
        or len(set(existing_target_ids)) != len(existing_target_ids)
        or workspace_id in existing_target_ids
        or (existing_target_ids and set(existing_target_ids) != source_target_ids)
        or not isinstance(existing_workflow_ids, list)
        or any(
            not isinstance(item, str) or not WORKFLOW_ID_PATTERN.fullmatch(item)
            for item in existing_workflow_ids
        )
        or len(set(existing_workflow_ids)) != len(existing_workflow_ids)
        or (existing_workflow_ids and sorted(existing_workflow_ids) != expected_workflow_ids)
    ):
        raise AllowlistGenerationError("workflow rerun action has invalid target or workflow IDs")
    return rerun_action


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    """Replace one private JSON output atomically with owner-only permissions."""

    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise AllowlistGenerationError(
            "workflow allowlist output must not be a symlink or directory"
        )
    encoded = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise AllowlistGenerationError(
            "failed to write the owner-only workflow allowlist"
        ) from error


def generate_workflow_allowlist(artifact_directory: Path) -> dict[str, Any]:
    """Derive provider, target, and WorkFlow v2 rerun scopes from one verified inventory."""

    artifact_root = _validate_artifact_directory(artifact_directory)
    config_directory = artifact_root / "config"
    inventory_path = artifact_root / WORKFLOW_INVENTORY_NAME
    targets_path = config_directory / TARGETS_NAME
    workflow_path = artifact_root / WORKFLOW_NAME

    inventory = _read_private_json(
        inventory_path, artifact_root, "verified source-workflow inventory"
    )
    targets_document = _read_private_json(
        targets_path, artifact_root, "Navigator responsibility targets"
    )
    workflow_document = _read_private_json(
        workflow_path, artifact_root, "Navigator WorkFlow v2 definition"
    )
    source_workflows = _validate_inventory(inventory)
    source_labels = set(source_workflows)
    _targets, target_by_label = _validate_targets(targets_document, source_labels)
    for label, workflow_ids in source_workflows.items():
        grant = next(
            row
            for row in target_by_label[label]["actionGrants"]
            if isinstance(row, dict) and row.get("id") == "rerun_source_ci"
        )
        existing_ids = grant.get("sourceWorkflowIds")
        if (
            not isinstance(existing_ids, list)
            or any(
                not isinstance(workflow_id, str) or not WORKFLOW_ID_PATTERN.fullmatch(workflow_id)
                for workflow_id in existing_ids
            )
            or len(set(existing_ids)) != len(existing_ids)
            or (existing_ids and existing_ids != workflow_ids)
        ):
            raise AllowlistGenerationError(
                "existing Navigator rerun grant has missing, extra, or mismatched source workflow IDs"
            )
    rerun_action = _validate_workflow_document(workflow_document, target_by_label, source_workflows)

    provider_source_workflow_ids: dict[str, list[str]] = {}
    source_workflow_union: list[str] = []
    for label in source_workflows:
        workflow_ids = source_workflows[label]
        repository_id = target_by_label[label]["id"]
        provider_source_workflow_ids[repository_id] = workflow_ids
        source_workflow_union.extend(workflow_ids)
        grant = next(
            row
            for row in target_by_label[label]["actionGrants"]
            if isinstance(row, dict) and row.get("id") == "rerun_source_ci"
        )
        grant["sourceWorkflowIds"] = workflow_ids

    rerun_action["targetIds"] = [target_by_label[label]["id"] for label in source_workflows]
    rerun_action["sourceWorkflowIds"] = sorted(source_workflow_union)
    provider_fragment = {"source_workflow_ids": provider_source_workflow_ids}

    provider_fragment_path = config_directory / PROVIDER_FRAGMENT_NAME
    if provider_fragment_path.exists():
        existing_fragment = _read_private_json(
            provider_fragment_path, artifact_root, "existing GitHub provider workflow fragment"
        )
        if existing_fragment != provider_fragment:
            raise AllowlistGenerationError(
                "existing GitHub provider workflow map has missing, extra, or mismatched IDs"
            )

    _atomic_json(provider_fragment_path, provider_fragment)
    _atomic_json(targets_path, targets_document)
    _atomic_json(workflow_path, workflow_document)
    inventory_digest = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    return {
        "repositoryCount": len(source_workflows) + 1,
        "sourceWorkflowRepositoryCount": len(source_workflows),
        "workflowIdCount": len(source_workflow_union),
        "inventorySha256": inventory_digest,
        "providerFragment": str(config_directory / PROVIDER_FRAGMENT_NAME),
        "targetsFile": str(targets_path),
        "workflowFile": str(workflow_path),
    }


def main() -> int:
    """Generate matching private allowlist outputs for an existing task artifact directory."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = generate_workflow_allowlist(arguments.artifact_dir)
    except (OSError, AllowlistGenerationError) as error:
        print(f"GitHub workflow allowlist generation blocked: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
