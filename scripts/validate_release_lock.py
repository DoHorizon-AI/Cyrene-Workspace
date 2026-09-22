"""
┌─────────────────────────────────────────────────────────────────────┐
│ Module: validate_release_lock                                       │
│ Role: Validate the single RC release lock before packaging.          │
│ 模块职责：打包前校验统一发布锁：环境、引擎、插件、仓库修订与验收模型。       │
└─────────────────────────────────────────────────────────────────────┘

The lock is the only authority for engine versions and acceptance fixtures.
A structural error is fatal; a non-terminal status is a named blocker that the
RC gate must resolve with real acceptance evidence before publication.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = WORKSPACE_ROOT / "release-lock.json"
LIFECYCLE_SOURCES_PATH = WORKSPACE_ROOT / "ci" / "text-lifecycle-v1" / "sources.json"

REPOSITORIES = (
    "Cyrene-Platform",
    "Cyrene-Plugins-Official",
    "Cyrene-Workspace",
    "Cyrene-Reactor",
    "Cyrene-Yield",
    "Cyrene-Exchange",
    "Cyrene-Catalyst",
    "Cyrene-Echo",
    "Cyrene-Navigator",
)
TERMINAL_STATUSES = {"PINNED", "RECORDED_AT_INSTALL"}
BLOCKING_STATUSES = {"PENDING_REAL_ACCEPTANCE", "EVIDENCE_SCOPED", "UNRESOLVED"}
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+")


def validate(document: dict[str, object]) -> tuple[list[str], list[str]]:
    """Return structural errors and named blockers. | 返回结构错误与阻塞项。"""

    errors: list[str] = []
    blockers: list[str] = []
    if document.get("schemaVersion") != 1:
        errors.append("schemaVersion must be 1")
    if not isinstance(document.get("name"), str) or not document["name"]:
        errors.append("name is required")

    model = document.get("acceptanceModel")
    if not isinstance(model, dict):
        errors.append("acceptanceModel is required")
    else:
        if not COMMIT_PATTERN.fullmatch(str(model.get("revision", ""))):
            errors.append("acceptanceModel.revision must be a pinned 40-hex commit")
        for field in ("repository", "license"):
            if not model.get(field):
                errors.append(f"acceptanceModel.{field} is required")

    if not isinstance(document.get("supportedEnvironment"), dict):
        errors.append("supportedEnvironment is required")
    python = document.get("python")
    if not isinstance(python, dict) or not python.get("runtime"):
        errors.append("python.runtime is required")
    if not isinstance(document.get("cuda"), dict):
        errors.append("cuda is required")

    engines = document.get("engines")
    if not isinstance(engines, dict) or not engines:
        errors.append("engines must declare at least one engine")
    else:
        for capability in ("training.llama-factory.v1", "execution.engine.v1"):
            engine = engines.get(capability)
            if not isinstance(engine, dict):
                errors.append(f"engines.{capability} is required")
                continue
            version = str(engine.get("acceptedVersion", ""))
            if not VERSION_PATTERN.match(version):
                errors.append(f"engines.{capability}.acceptedVersion must be a version")
            if not engine.get("license"):
                errors.append(f"engines.{capability}.license is required")
            status = str(engine.get("status", ""))
            if status in BLOCKING_STATUSES:
                blockers.append(f"engine {capability} is {status}")
            elif status not in TERMINAL_STATUSES:
                errors.append(f"engines.{capability}.status is not a known status")

    plugins = document.get("plugins")
    if not isinstance(plugins, dict) or not plugins:
        errors.append("plugins must declare the RC plugin packages")

    revisions = document.get("repositories")
    if not isinstance(revisions, dict):
        errors.append("repositories is required")
    else:
        for name in REPOSITORIES:
            value = str(revisions.get(name, ""))
            if not COMMIT_PATTERN.fullmatch(value):
                errors.append(f"repositories.{name} must be a pinned 40-hex commit")
    return errors, blockers


def validate_lifecycle_sources(
    document: dict[str, object], sources: dict[str, object]
) -> list[str]:
    """Require lifecycle tests to use the revisions promoted by the RC lock."""

    errors: list[str] = []
    revisions = document.get("repositories")
    products = sources.get("products")
    if not isinstance(revisions, dict):
        return ["repositories is required before lifecycle sources can be validated"]
    if not isinstance(products, dict):
        return ["lifecycle sources.products is required"]

    expected = {"Cyrene-Platform": sources.get("platformRevision")}
    expected.update({f"Cyrene-{name}": revision for name, revision in products.items()})
    for repository, revision in expected.items():
        if repository not in REPOSITORIES:
            errors.append(f"lifecycle sources declare unknown repository {repository}")
        elif revisions.get(repository) != revision:
            errors.append(f"lifecycle source {repository} must match repositories.{repository}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument(
        "--lifecycle-sources",
        type=Path,
        default=LIFECYCLE_SOURCES_PATH,
    )
    parser.add_argument(
        "--require-terminal",
        action="store_true",
        help="Fail when the lock still carries non-terminal engine statuses",
    )
    arguments = parser.parse_args()
    try:
        document = json.loads(arguments.lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"status": "ERROR", "code": "RELEASE_LOCK_UNREADABLE", "detail": str(exc)})
        )
        return 1
    if not isinstance(document, dict):
        print(json.dumps({"status": "ERROR", "code": "RELEASE_LOCK_INVALID"}))
        return 1
    try:
        lifecycle_sources = json.loads(arguments.lifecycle_sources.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"status": "ERROR", "code": "LIFECYCLE_SOURCES_UNREADABLE", "detail": str(exc)}
            )
        )
        return 1
    if not isinstance(lifecycle_sources, dict):
        print(json.dumps({"status": "ERROR", "code": "LIFECYCLE_SOURCES_INVALID"}))
        return 1
    errors, blockers = validate(document)
    errors.extend(validate_lifecycle_sources(document, lifecycle_sources))
    status = (
        "ERROR" if errors else ("BLOCKED" if blockers and arguments.require_terminal else "VALID")
    )
    print(
        json.dumps(
            {"status": status, "errors": errors, "blockers": blockers},
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if errors or (blockers and arguments.require_terminal) else 0


if __name__ == "__main__":
    raise SystemExit(main())
