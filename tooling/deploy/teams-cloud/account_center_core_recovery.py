"""Restore an unmigrated Account Center after a partial Graph bootstrap.

This operator-only tool preserves the failed operation and the upgraded
Persistence container. It never restores database bytes or promotes a selector.
中文：仅恢复已证明仍为旧 schema 的授权服务，保留原失败记录与新持久化服务。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any

import stage_transition as st

SCHEMA = "cyrene-account-center-core-recovery-v1"
RESTORED = "CORE_RESTORED_MIXED_PENDING_NEW_PLAN"
PROFILE_COLUMNS = (
    "profile_ref,anchor_issuer,anchor_subject,organization_id,workspace_id,"
    "provider,identity_ref,target_scope_json,external_subject,status,auth_mode"
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise st.StageTransitionError(message)


def _digest(document: Any) -> str:
    return hashlib.sha256(st._canonical_json(document)).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    """Read a root-owned private record without following a symlink."""

    _require(st.preflight._private_artifact(path), "record must be a private artifact")
    _require(path.is_file() and not path.is_symlink(), "record is not a regular file")
    metadata = path.stat()
    _require(
        metadata.st_uid == os.geteuid() and stat.S_IMODE(metadata.st_mode) == 0o600,
        "record must be current-user-owned mode 0600",
    )
    _require(metadata.st_size <= 4 * 1024 * 1024, "record exceeds the bounded size")
    result = json.loads(path.read_bytes())
    _require(isinstance(result, dict), "record must be an object")
    return result


def _file(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": st._sha256_bytes(path.read_bytes())}


def _implementation() -> str:
    """Bind the recovery implementation and both existing operator helper modules."""

    return _digest(
        [
            _file(path)
            for path in (Path(__file__), Path(st.__file__), Path(st.runtime_update.__file__))
        ]
    )


def _inventory(project: str, docker: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return stable container identities and private inspection data, without logs."""

    ids = subprocess.check_output(
        [*docker, "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        text=True,
        timeout=20,
    ).split()
    _require(bool(ids), "project containers are absent")
    raw = json.loads(subprocess.check_output([*docker, "inspect", *ids], text=True, timeout=20))
    identities: dict[str, Any] = {}
    containers: dict[str, Any] = {}
    for container in raw:
        name = container["Config"]["Labels"]["com.docker.compose.service"]
        _require(name not in identities, "project has duplicate service containers")
        identities[name] = {
            "id": container["Id"],
            "imageId": container["Image"],
            "configSha256": _digest(container["Config"]),
            # Docker inspect does not promise mount ordering; every binding still participates.
            # 中文：仅规范展示顺序，源、目标、模式等字段仍全部参与校验。
            "mountsSha256": _digest(sorted(container["Mounts"], key=st._canonical_json)),
            "hostConfigSha256": _digest(container["HostConfig"]),
        }
        containers[name] = container
    return identities, containers


def _database(path: Path) -> dict[str, Any]:
    """Read a consistent v8 schema and opaque row digests; never write the database."""

    _require(path.is_file() and not path.is_symlink(), "live database path is unsafe")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        connection.execute("BEGIN")
        schema = list(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        )
        tables = []
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        _require(len(names) <= 100, "unexpected database table count")
        for name in names:
            _require(name.replace("_", "").isalnum(), "unsafe database table name")
            digest = hashlib.sha256()
            count = 0
            for row in connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
                values = [
                    {"bytesSha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
                    if isinstance(value, bytes)
                    else value
                    for value in row
                ]
                digest.update(
                    json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
                )
                count += 1
                _require(count <= 100000, "database reconciliation exceeds the row bound")
            tables.append({"table": name, "rows": count, "rowDigestSha256": digest.hexdigest()})
        profiles = list(
            connection.execute(
                f"SELECT {PROFILE_COLUMNS} FROM provider_profiles ORDER BY profile_ref"
            )
        )
        return {
            "userVersion": connection.execute("PRAGMA user_version").fetchone()[0],
            "schemaSha256": hashlib.sha256(
                json.dumps(schema, separators=(",", ":")).encode()
            ).hexdigest(),
            "tables": tables,
            "profilesIdentitySha256": _digest(profiles),
            "providers": [
                {"provider": row[0], "count": row[1]}
                for row in connection.execute(
                    "SELECT provider,count(*) FROM provider_profiles GROUP BY provider ORDER BY provider"
                )
            ],
            "foreignKeyViolationCount": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
            "quickCheck": [row[0] for row in connection.execute("PRAGMA quick_check")],
        }
    finally:
        connection.close()


def _database_path(container: dict[str, Any]) -> Path:
    """Resolve the actual mounted default database on either candidate or restored AC."""

    mounts = [
        row
        for row in container["Mounts"]
        if row["Type"] == "volume" and row["Destination"] == "/data"
    ]
    _require(len(mounts) == 1, "Account Center data volume is ambiguous")
    environment = dict(value.split("=", 1) for value in container["Config"]["Env"] if "=" in value)
    activation = json.loads(environment["CYRENE_CAPABILITY_CONFIGURATION_JSON"])
    _require(
        environment.get("CYRENE_PLUGIN_STORAGE_PATH") == "/data"
        and activation.get("storage_path", "/data/accounts.sqlite3") == "/data/accounts.sqlite3",
        "unsupported live database configuration",
    )
    return Path(mounts[0]["Source"]) / "accounts.sqlite3"


def _holds(source: dict[str, Any], journal: dict[str, Any], docker: list[str]) -> dict[str, Any]:
    """Verify the exact original pause revision and Navigator drain generation."""

    edge = st.runtime_update._current_edge_status(docker, journal["edge"]["containerId"])
    nav = st.runtime_update._current_navigator_status(
        docker, journal["navigator"]["initialContainerId"]
    )
    _require(
        edge["paused"] is True and edge["revision"] == journal["edge"]["pauseRevision"],
        "original ingress pause is not held",
    )
    _require(
        edge["dead_count"] == journal["edge"]["initialDeadCount"],
        "ingress dead-letter count changed",
    )
    _require(
        journal["edge"]["pauseHolder"] == source["operationId"],
        "ingress lease belongs to another operation",
    )
    _require(
        journal["edge"]["pauseIntent"] is True
        and journal["edge"]["pauseRevision"] == journal["edge"]["initialRevision"] + 1
        and journal["edge"]["containerId"] == source["edgeDelivery"]["containerId"],
        "ingress pause intent or container differs",
    )
    _require(
        nav["mode"] == "draining"
        and nav["instanceId"] == journal["navigator"]["initialInstanceId"],
        "original Navigator drain is not held",
    )
    _require(
        nav["generation"] == journal["navigator"]["initialGeneration"] + 1,
        "Navigator generation changed",
    )
    _require(
        nav["activeWorkDrained"] is True
        and nav["activeTasks"] == 0
        and nav["dispatches"] == {"active": 0, "waiting": 0}
        and nav["notifications"]["pollingPaused"] is True
        and nav["notifications"]["inFlight"] is False,
        "Navigator has not drained active work",
    )
    _require(
        source["activeServices"]["navigator"]["containerId"]
        == journal["navigator"]["initialContainerId"],
        "Navigator container binding changed",
    )
    return {
        "edgeRevision": edge["revision"],
        "navigatorInstanceId": nav["instanceId"],
        "navigatorGeneration": nav["generation"],
    }


def _context(paths: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Reconcile only the closed partial-bootstrap shape supported by this recovery."""

    source = _read(Path(paths["sourcePlan"]["path"]))
    st._validate_plan(source)
    st._verify_all_inputs(source)
    st._check_selection_unchanged(source)
    for reference in paths.values():
        _require(_file(Path(reference["path"])) == reference, "recovery input changed")
    journal = st._read_journal(Path(paths["sourceJournal"]["path"]))
    evidence = _read(Path(paths["databaseEvidence"]["path"]))
    compatibility = _read(Path(paths["compatibilityEvidence"]["path"]))
    _require(
        source["phase"] == st.OFFICE_GRAPH_PROVIDER_BOOTSTRAP_PHASE,
        "source is not a Graph bootstrap",
    )
    _require(
        journal["operationId"] == source["operationId"] and journal["state"] == "RECOVERY_REQUIRED",
        "source operation is not held for recovery",
    )
    _require(
        journal["planSha256"] == source["planSha256"], "source journal belongs to another plan"
    )
    _require(
        journal["planPath"] == paths["sourcePlan"]["path"]
        and journal["project"] == source["project"]
        and journal["targets"] == source["targets"],
        "source journal identity differs from its sealed plan",
    )
    _require(
        journal["lastErrorCode"] == st.ACCOUNT_CENTER_DOWNGRADE_BLOCKED_ERROR,
        "unsupported source failure",
    )
    _require(
        journal["selection"]["promoted"] is False
        and journal["selection"]["promotionIntent"] is False,
        "selection promotion may have started",
    )
    progress = journal["targetProgress"]
    _require(
        progress["navigator-persistence"]["phase"] == "APPLIED", "Persistence was not fully applied"
    )
    _require(
        progress["navigator-persistence"]["applyIntent"] is False
        and progress["navigator-persistence"]["candidateContainerId"]
        == progress["navigator-persistence"]["healthyContainerId"],
        "Persistence application is unresolved",
    )
    _require(
        progress["account-center"]["applyIntent"] is True,
        "Account Center replacement was not attempted",
    )
    _require(
        progress["account-center"]["phase"] == "CONTAINER_READBACK"
        and progress["account-center"]["rollbackIntent"] is False
        and progress["account-center"]["rollbackContainerId"] is None,
        "Account Center replacement is unresolved",
    )
    for service, state in progress.items():
        if service not in {"account-center", "navigator-persistence"}:
            _require(
                state["phase"] == "PREPARED"
                and not state["applyIntent"]
                and state["candidateContainerId"] is None,
                "another target may have changed",
            )
    docker = source["policy"]["dockerPrefix"]
    st.runtime_update._validate_docker_prefix(docker)
    identities, containers = _inventory(source["project"], docker)
    _require(set(identities) == set(source["activeServices"]), "runtime service set changed")
    for service, identity in identities.items():
        expected = source["activeServices"][service]
        if service == "account-center":
            _require(
                identity["id"] == progress[service]["candidateContainerId"]
                and identity["imageId"] == progress[service]["candidateImageId"],
                "Account Center candidate container or image changed",
            )
        else:
            if service == "navigator-persistence":
                expected = {
                    "containerId": progress[service]["candidateContainerId"],
                    "imageId": progress[service]["candidateImageId"],
                }
            _require(
                identity["id"] == expected["containerId"]
                and identity["imageId"] == expected["imageId"],
                "non-target runtime identity changed",
            )
            _require(
                containers[service]["State"]["Running"]
                and containers[service]["State"].get("Health", {}).get("Status")
                in (None, "healthy"),
                "non-target service is unhealthy",
            )
    path = _database_path(containers["account-center"])
    _require(
        str(path) == evidence["databasePath"], "database evidence references a different volume"
    )
    database = _database(path)
    for key in (
        "userVersion",
        "schemaSha256",
        "tables",
        "providers",
        "foreignKeyViolationCount",
        "quickCheck",
    ):
        _require(
            database[key] == evidence[key], "live database differs from the read-only evidence"
        )
    _require(
        database["userVersion"] == 8
        and database["foreignKeyViolationCount"] == 0
        and database["quickCheck"] == ["ok"],
        "database is not a healthy unmigrated v8 store",
    )
    _require(
        compatibility.get("state") == "OLD_ACCOUNT_CENTER_DATABASE_COMPATIBLE"
        and compatibility.get("logicalDatabaseUnchanged") is True
        and compatibility.get("oldImageSchemaVersion") == 8,
        "old-image compatibility is not proven",
    )
    _require(
        compatibility.get("foreignKeyViolationCount") == 0
        and compatibility.get("tableDigestsMatchLiveReadonlyReceipt") is True
        and compatibility.get("liveDatabaseMounted") is False
        and compatibility.get("secretsMounted") is False
        and compatibility.get("network") == "none",
        "old-image compatibility test scope is invalid",
    )
    _require(
        compatibility["imageId"] == progress["account-center"]["oldImageId"]
        and compatibility["snapshotSha256"] == evidence["snapshotSha256"]
        and compatibility["schemaSha256"] == database["schemaSha256"],
        "old-image evidence binding differs",
    )
    snapshot = Path(evidence["snapshotPath"])
    _require(snapshot.is_file() and not snapshot.is_symlink(), "private snapshot path is unsafe")
    with snapshot.open("rb") as stream:
        _require(
            hashlib.file_digest(stream, "sha256").hexdigest() == evidence["snapshotSha256"],
            "private snapshot digest changed",
        )
    old_image = source["candidateServices"]["account-center"]["active"]
    resolved = json.loads(
        subprocess.check_output(
            [*docker, "image", "inspect", old_image["imageRef"]], text=True, timeout=20
        )
    )
    _require(
        len(resolved) == 1
        and resolved[0]["Id"] == old_image["imageId"] == progress["account-center"]["oldImageId"],
        "old image reference no longer resolves to its immutable ID",
    )
    holds = _holds(source, journal, docker)
    return (
        source,
        journal,
        {"containers": identities, "databasePath": str(path), "database": database, "holds": holds},
    )


def prepare(
    source_path: Path, evidence_path: Path, compatibility_path: Path, output: Path
) -> dict[str, Any]:
    """Seal an operator-reviewable recovery plan; perform no runtime mutation."""

    st.runtime_update._require_update_operator()
    source = _read(source_path)
    st._validate_plan(source)
    journal_path = st._journal_path(Path(source["journalDir"]), source["operationId"])
    paths = {
        "sourcePlan": _file(source_path),
        "sourceJournal": _file(journal_path),
        "databaseEvidence": _file(evidence_path),
        "compatibilityEvidence": _file(compatibility_path),
    }
    with st.runtime_update._operation_lock(source["project"]):
        _, _, baseline = _context(paths)
        result = {
            "schemaVersion": SCHEMA,
            "operationId": str(uuid.uuid4()),
            "project": source["project"],
            "sourceOperationId": source["operationId"],
            "createdAtUtc": st._utc_now(),
            "implementationSha256": _implementation(),
            "inputs": paths,
            "baseline": baseline,
            "resultState": RESTORED,
        }
        result["planSha256"] = _digest(result)
        st.runtime_update._ensure_journal_directory(output.parent, create=True)
        st._create_initial_journal(output, result)
    return result


def apply(plan_path: Path, expected_digest: str) -> dict[str, Any]:
    """Restore only old AC, then release the original holds; never replay an intent.

    中文：独立记账；原 plan、journal、selector 和数据库快照均不会改写。
    """

    st.runtime_update._require_update_operator()
    recovery = _read(plan_path)
    unsigned = {key: value for key, value in recovery.items() if key != "planSha256"}
    _require(
        recovery.get("schemaVersion") == SCHEMA
        and recovery.get("resultState") == RESTORED
        and _digest(unsigned) == recovery.get("planSha256") == expected_digest,
        "recovery confirmation differs from sealed plan",
    )
    _require(
        recovery.get("implementationSha256") == _implementation(),
        "recovery implementation changed after planning",
    )
    journal_path = plan_path.parent / f"{recovery['operationId']}.journal.json"
    with st.runtime_update._operation_lock(recovery["project"]):
        _require(
            not journal_path.exists() and not journal_path.is_symlink(),
            "recovery intent exists; do not replay",
        )
        source, original, baseline = _context(recovery["inputs"])
        _require(
            source["project"] == recovery["project"]
            and source["operationId"] == recovery["sourceOperationId"],
            "recovery lock or source operation differs",
        )
        _require(baseline == recovery["baseline"], "runtime changed after recovery planning")
        journal = copy.deepcopy(original)
        journal.update(
            schemaVersion=SCHEMA + "-journal",
            operationId=recovery["operationId"],
            planPath=str(plan_path),
            planSha256=expected_digest,
            sourceOperationId=source["operationId"],
            sourcePlanSha256=source["planSha256"],
            sourceJournalSha256=recovery["inputs"]["sourceJournal"]["sha256"],
            recoverySchemaVersion=SCHEMA,
            recoveryPlanSha256=expected_digest,
            state="CORE_RESTORE_REQUESTED",
        )
        st._create_initial_journal(journal_path, journal)
        docker = source["policy"]["dockerPrefix"]
        try:
            command = st._compose_target_update(
                plan=source, service="account-center", use_candidate=False, docker_prefix=docker
            )
            _require(
                command.returncode == 0,
                "Account Center restore command failed; reconcile without replay",
            )
            restored = st._wait_for_target_service(
                project=source["project"],
                service_name="account-center",
                expected_image=source["candidateServices"]["account-center"]["active"],
                has_healthcheck=True,
                timeout_seconds=source["policy"]["healthTimeoutSeconds"],
                docker_prefix=docker,
            )
            identities, containers = _inventory(source["project"], docker)
            _require(
                set(identities) == set(baseline["containers"]),
                "service set changed during recovery",
            )
            for service, identity in identities.items():
                if service != "account-center":
                    _require(
                        identity == baseline["containers"][service],
                        "another container changed during recovery",
                    )
                _require(
                    containers[service]["State"]["Running"]
                    and containers[service]["State"].get("Health", {}).get("Status")
                    in (None, "healthy"),
                    "core runtime is unhealthy",
                )
            path = _database_path(containers["account-center"])
            _require(
                str(path) == baseline["databasePath"],
                "restored Account Center mounted a different database",
            )
            database = _database(path)
            for key in (
                "userVersion",
                "schemaSha256",
                "tables",
                "profilesIdentitySha256",
                "providers",
                "foreignKeyViolationCount",
                "quickCheck",
            ):
                _require(
                    database[key] == baseline["database"][key],
                    "database identity or integrity changed during recovery",
                )
            st._check_selection_unchanged(source)
            _require(
                _file(Path(recovery["inputs"]["sourceJournal"]["path"]))
                == recovery["inputs"]["sourceJournal"],
                "source journal changed during recovery",
            )
            st._resume_old_executor_if_needed(
                plan=source, journal=journal, docker_prefix=docker, require_original_instance=True
            )
            st._resume_ingress_after_safe_state(
                journal=journal, journal_path=journal_path, docker_prefix=docker
            )
            delivery = st._verify_final_delivery_readback(journal=journal, docker_prefix=docker)
            st._check_selection_unchanged(source)
            journal["coreRecovery"] = {
                "state": RESTORED,
                "restoredAccountCenter": restored,
                "retainedPersistence": identities["navigator-persistence"],
                "runtimeContainers": identities,
                "databaseSchemaVersion": 8,
                "profileIdentitiesPreserved": True,
                "databaseRestorePerformed": False,
                "originalSelectorUnchanged": True,
                "officeEnabled": False,
                "deliveryPaused": delivery["paused"],
                "deadCount": delivery["dead_count"],
            }
            st._write_journal(journal_path, journal, RESTORED, state=RESTORED)
            return {
                "state": RESTORED,
                "operationId": recovery["operationId"],
                "sourceOperationId": source["operationId"],
                "journalPath": str(journal_path),
                "coreRecovery": journal["coreRecovery"],
            }
        except Exception:
            st._write_journal(
                journal_path,
                journal,
                "CORE_RESTORE_REQUIRES_RECONCILIATION",
                state="RECOVERY_REQUIRED",
            )
            raise


def main() -> int:
    """Expose explicit prepare/apply commands for the existing VM operator lane."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    for name in ("source-plan", "database-evidence", "compatibility-evidence", "output"):
        prepare_parser.add_argument(f"--{name}", type=Path, required=True)
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--confirm-plan-sha256", required=True)
    args = parser.parse_args()
    try:
        result = (
            prepare(
                args.source_plan, args.database_evidence, args.compatibility_evidence, args.output
            )
            if args.command == "prepare"
            else apply(args.plan, args.confirm_plan_sha256)
        )
    except (
        st.StageTransitionError,
        st.runtime_update.RuntimeUpdateError,
        OSError,
        ValueError,
        KeyError,
        sqlite3.Error,
    ) as error:
        print(
            json.dumps(
                {"state": "BLOCKED", "errorType": type(error).__name__, "message": str(error)[:300]}
            )
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
