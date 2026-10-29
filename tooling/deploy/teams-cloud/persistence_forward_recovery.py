"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 persistence_forward_recovery.py                                      │
│  Module: tooling.deploy.teams_cloud.persistence_forward_recovery          │
│  Role: Prove and commit one source-pinned Persistence SQLite migration.  │
│                                                                          │
│  模块职责：验证唯一受支持的持久化迁移故障，并安全向前完成切换。          │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import preflight
import runtime_update
import stage_transition

RECOVERY_SCHEMA = "cyrene-teams-persistence-forward-recovery-v1"
PREVIEW_SCHEMA = "cyrene-teams-persistence-forward-recovery-preview-v1"
TARGET = stage_transition.PERSISTENCE_IMAGE_UPDATE_TARGET
SUPPORTED_TARGETS = [TARGET]
SUPPORTED_ERROR = "database_migration_contract_failed"
SUPPORTED_NAVIGATOR_COMMIT = "ca805f9f322de4d08e5ff2af54b1dc7613213092"
SUPPORTED_SOURCE_FILES = {
    "src/cyrene_navigator/persistence/store.py": "a605b9d174109d05ed97177f6da359938880b69d0ae1fa4d591261ed3e24546f",
    "src/cyrene_navigator/work/store.py": "cd3db0699ed2ebf786ecf7321940f72790fbf362a33bbda1f19f297e13fb7a24",
    "src/cyrene_navigator/work/api.py": "3f38a9b830d439876e378644b9a2ecc17b339dfb0ed0c3ae0f16a610684ca2d6",
    "src/cyrene_navigator/persistence/api.py": "b60a39673b76663576a6ebec5eff8ca5b0ccc7080e7a43943419f18190d8fb84",
}


class ForwardRecoveryError(ValueError):
    """The one supported source-pinned Persistence recovery cannot proceed safely."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _private_regular(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ForwardRecoveryError(f"{label} is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or path.is_symlink()
        or path.resolve(strict=True) != path
    ):
        raise ForwardRecoveryError(f"{label} must be an owner-only regular file")


def _private_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir() or path.resolve(strict=True) != path:
        raise ForwardRecoveryError(f"{label} is not a canonical directory")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ForwardRecoveryError(f"{label} must be owner-only")


def _write_exclusive(path: Path, value: dict[str, Any]) -> str:
    """Persist one immutable private preview/authorization receipt."""

    _private_directory(path.parent, "recovery artifact directory")
    payload = _canonical_json(value) + b"\n"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return _sha256_bytes(payload)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    _private_regular(path, label)
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=preflight._unique_json_object
        )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ForwardRecoveryError(f"{label} is malformed") from error
    if not isinstance(value, dict):
        raise ForwardRecoveryError(f"{label} must be a JSON object")
    return value


def _load_context(
    journal_dir: Path, operation_id: str
) -> tuple[dict[str, Any], dict[str, Any], Path, Path]:
    runtime_update._require_update_operator()
    runtime_update._ensure_journal_directory(journal_dir, create=False)
    journal_path = stage_transition._journal_path(journal_dir, operation_id)
    journal = stage_transition._read_journal(journal_path)
    plan_path = Path(journal.get("planPath", ""))
    _private_regular(plan_path, "sealed stage transition plan")
    plan = stage_transition._read_json(plan_path, "stage transition plan")
    stage_transition._validate_plan(plan)
    if (
        plan.get("operationId") != operation_id
        or journal.get("operationId") != operation_id
        or plan.get("planSha256") != journal.get("planSha256")
        or Path(plan.get("journalDir", "")) != journal_dir.resolve(strict=True)
    ):
        raise ForwardRecoveryError("the exact sealed plan and operation journal do not match")
    _validate_supported_failure(plan, journal)
    return plan, journal, plan_path, journal_path


def _validate_supported_failure(plan: dict[str, Any], journal: dict[str, Any]) -> None:
    """Restrict this API to the witnessed Persistence same-schema gate failure."""

    progress = journal.get("targetProgress")
    persistence = progress.get(TARGET) if isinstance(progress, dict) else None
    if (
        plan.get("imageUpdateTarget") != TARGET
        or plan.get("phase") != "issued-connector-activation"
        or plan.get("targets") != SUPPORTED_TARGETS
        or journal.get("state") != "RECOVERY_REQUIRED"
        or journal.get("lastErrorCode") != SUPPORTED_ERROR
        or journal.get("persistenceForwardRecovery") is not None
        or journal.get("database", {}).get("migrationGateFailed") is not True
        or not isinstance(persistence, dict)
        or persistence.get("phase") != "APPLIED"
        or persistence.get("applyIntent") is not False
        or persistence.get("rollbackIntent") is not False
        or persistence.get("candidateContainerId") != persistence.get("healthyContainerId")
        or not isinstance(persistence.get("healthyContainerId"), str)
        or journal.get("selection", {}).get("promoted") is not False
        or journal.get("selection", {}).get("promotionIntent") is not False
    ):
        raise ForwardRecoveryError("this journal is outside the single supported recovery case")
    if set(progress) != set(SUPPORTED_TARGETS):
        raise ForwardRecoveryError("the operation target journal is not the supported closed set")
    database = journal.get("database")
    if (
        not isinstance(database, dict)
        or database.get("restorePerformed") is not False
        or not isinstance(database.get("backupPath"), str)
        or not isinstance(database.get("backupSha256"), str)
        or not isinstance(database.get("snapshot"), dict)
    ):
        raise ForwardRecoveryError("the original durable database backup evidence is incomplete")


def _candidate_image(plan: dict[str, Any], docker_prefix: list[str]) -> dict[str, str]:
    lock_path = Path(plan["candidateInputs"]["imageLock"]["path"])
    lock = runtime_update._load_lock(lock_path)
    source = lock.get("sources", {}).get("navigator", {})
    rows = runtime_update._image_rows_by_service(lock)
    target = rows.get(TARGET)
    candidate = plan.get("candidateServices", {}).get(TARGET, {}).get("candidate")
    if (
        not isinstance(source, dict)
        or source.get("commit") != SUPPORTED_NAVIGATOR_COMMIT
        or not isinstance(target, dict)
        or target.get("sourceCommits", {}).get("navigator") != SUPPORTED_NAVIGATOR_COMMIT
        or not isinstance(candidate, dict)
        or candidate.get("imageRef") != target.get("image")
        or candidate.get("imageId") != target.get("imageId")
    ):
        raise ForwardRecoveryError("the candidate is not the pinned Navigator source/image")
    inspected = runtime_update._decode_json_output(
        runtime_update._checked_stdout(
            [*docker_prefix, "image", "inspect", candidate["imageRef"]],
            label="candidate Persistence image provenance",
        ),
        "candidate Persistence image inspection",
    )
    labels = (
        inspected[0].get("Config", {}).get("Labels", {})
        if isinstance(inspected, list) and len(inspected) == 1
        else {}
    )
    if (
        not isinstance(inspected, list)
        or len(inspected) != 1
        or inspected[0].get("Id") != candidate["imageId"]
        or inspected[0].get("Os") != "linux"
        or inspected[0].get("Architecture") not in {"arm64", "aarch64"}
        or labels.get("io.cyrene.source.navigator.revision") != SUPPORTED_NAVIGATOR_COMMIT
    ):
        raise ForwardRecoveryError("the local candidate image ID/source label is not exact")
    return {"imageRef": candidate["imageRef"], "imageId": candidate["imageId"]}


def _report_projection(report: dict[str, Any]) -> dict[str, Any]:
    stage_transition._validate_sqlite_report_shape(report, "SQLite migration report")
    return {
        key: report[key]
        for key in (
            "integrity",
            "userVersion",
            "schemaSha256",
            "objects",
            "schemaObjects",
            "tables",
        )
    }


def _report_projection_without_reconciliation(report: dict[str, Any]) -> dict[str, Any]:
    """Compare a pristine backup report that has no derived old-column digests."""

    projection = _report_projection(report)
    projection["tables"] = {
        name: {
            key: value
            for key, value in table.items()
            if key not in {"preservedRowsCount", "preservedRowsDigest"}
        }
        for name, table in projection["tables"].items()
    }
    return projection


def _compare_reports(expected: dict[str, Any], actual: dict[str, Any], label: str) -> None:
    if _report_projection(expected) != _report_projection(actual):
        raise ForwardRecoveryError(f"{label} differs from exact source-migration read-back")


def _candidate_source_replay(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    image: dict[str, str],
    docker_prefix: list[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]]:
    """Run the exact image's PersistenceStore→WorkStore startup migration twice on a private copy."""

    snapshot = stage_transition._validate_sqlite_report_shape(
        journal["database"]["snapshot"], "original Persistence snapshot"
    )
    backup = Path(journal["database"]["backupPath"])
    _private_regular(backup, "original Persistence SQLite backup")
    backup_sha = _sha256_file(backup)
    if backup_sha != journal["database"]["backupSha256"]:
        raise ForwardRecoveryError("original SQLite backup does not match its journal digest")
    scratch_parent = backup.parent
    _private_directory(scratch_parent, "Persistence backup directory")
    with tempfile.TemporaryDirectory(
        prefix=".forward-recovery-", dir=scratch_parent
    ) as scratch_name:
        scratch = Path(scratch_name)
        os.chown(scratch, 10001, 10001)
        os.chmod(scratch, 0o700)
        database = scratch / "persistence.db"
        shutil.copyfile(backup, database)
        os.chown(database, 10001, 10001)
        os.chmod(database, 0o600)
        contract = stage_transition._sqlite_comparison_contract(snapshot)
        script = (
            stage_transition._SQLITE_REPORT_PYTHON
            + "\nfrom pathlib import Path\n"
            + "import hashlib,inspect\n"
            + "from cyrene_navigator.persistence.store import PersistenceStore\n"
            + "from cyrene_navigator.work.store import WorkStore\n"
            + "from cyrene_navigator.persistence.api import create_persistence_app\n"
            + "from cyrene_navigator.work.api import mount_work_routes\n"
            + "database=Path(__import__('sys').argv[1])\n"
            + "contract=json.loads(__import__('sys').argv[2])\n"
            + "source_pins=json.loads(__import__('sys').argv[3])\n"
            + "source_objects={'src/cyrene_navigator/persistence/store.py':PersistenceStore,'src/cyrene_navigator/work/store.py':WorkStore,'src/cyrene_navigator/work/api.py':mount_work_routes,'src/cyrene_navigator/persistence/api.py':create_persistence_app}\n"
            + "source_hashes={}\n"
            + "for name,obj in source_objects.items():\n"
            + " path=Path(inspect.getsourcefile(obj) or '')\n"
            + " if not path.is_file(): raise RuntimeError('pinned migration source file unavailable: '+name)\n"
            + " digest=hashlib.sha256(path.read_bytes()).hexdigest()\n"
            + " if digest!=source_pins.get(name): raise RuntimeError('candidate source hash mismatch: '+name)\n"
            + " source_hashes[name]=digest\n"
            + "before=report_database(str(database),contract)\n"
            + "PersistenceStore(database)\n"
            + "WorkStore(database,attachment_root=database.parent/'attachments')\n"
            + "after=report_database(str(database),contract)\n"
            + "PersistenceStore(database)\n"
            + "WorkStore(database,attachment_root=database.parent/'attachments')\n"
            + "again=report_database(str(database),contract)\n"
            + "for report in (before,after,again): report['objects']=sorted(report['objects'])\n"
            + "print(json.dumps({'before':before,'after':after,'again':again,'sourceHashes':source_hashes},sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False))\n"
        )
        contract_text = json.dumps(contract, sort_keys=True, separators=(",", ":"))
        command = [
            *docker_prefix,
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "32",
            "--memory",
            "256m",
            "--cpus",
            "0.25",
            "--user",
            "10001:10001",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=16777216",
            "--mount",
            f"type=bind,src={scratch},dst=/var/lib/cyrene-navigator",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint",
            "/opt/venv/bin/python",
            image["imageId"],
            "-c",
            script,
            "/var/lib/cyrene-navigator/persistence.db",
            contract_text,
            json.dumps(SUPPORTED_SOURCE_FILES, sort_keys=True, separators=(",", ":")),
        ]
        output = runtime_update._checked_stdout(
            command, label="isolated exact-image Persistence migration replay", timeout_seconds=180
        )
        result = runtime_update._decode_json_output(output, "isolated SQLite migration report")
        if not isinstance(result, dict) or set(result) != {
            "before",
            "after",
            "again",
            "sourceHashes",
        }:
            raise ForwardRecoveryError("isolated source-migration output is malformed")
        before = stage_transition._validate_sqlite_report_shape(
            result["before"], "replayed baseline"
        )
        after = stage_transition._validate_sqlite_report_shape(
            result["after"], "replayed migration"
        )
        again = stage_transition._validate_sqlite_report_shape(
            result["again"], "replayed idempotency"
        )
        if _report_projection_without_reconciliation(
            snapshot
        ) != _report_projection_without_reconciliation(before):
            raise ForwardRecoveryError(
                "replayed baseline differs from the sealed SQLite backup report"
            )
        _compare_reports(after, again, "second source migration pass")
        if after.get("integrity") != "ok":
            raise ForwardRecoveryError("source-migrated backup failed SQLite integrity_check")
        source_hashes = result["sourceHashes"]
        if source_hashes != SUPPORTED_SOURCE_FILES:
            raise ForwardRecoveryError("candidate image source hash read-back is incomplete")
        return before, after, again, source_hashes


def _mixed_runtime_readback(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    docker_prefix: list[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Verify all fourteen services at the precise post-Persistence/pre-next-target boundary."""

    stage_transition._verify_all_inputs(plan)
    stage_transition._check_selection_unchanged(plan)
    candidate_compose, candidate_lock = stage_transition._candidate_configs(plan, docker_prefix)
    live = stage_transition._live_service_inventory(
        compose=candidate_compose,
        image_lock=candidate_lock,
        docker_prefix=docker_prefix,
        mount_identity_by_service=plan["runtimeConfigIdentity"]["candidate"],
    )
    for name, expected in plan["activeServices"].items():
        if name == TARGET:
            continue
        if live.get(name) != expected:
            raise ForwardRecoveryError(f"untargeted service {name} changed before forward recovery")
    candidate = plan["candidateServices"][TARGET]["candidate"]
    progress = journal["targetProgress"][TARGET]
    current = live.get(TARGET)
    if (
        not isinstance(current, dict)
        or current.get("imageRef") != candidate["imageRef"]
        or current.get("imageId") != candidate["imageId"]
        or current.get("containerId") != progress.get("healthyContainerId")
        or current.get("health") != "healthy"
    ):
        raise ForwardRecoveryError("the healthy Persistence target no longer matches the journal")
    active_leases = stage_transition._active_persistence_writer_lease_count(
        docker_prefix=docker_prefix,
        container_id=current["containerId"],
        database_path=plan["database"]["containerPath"],
    )
    if active_leases != 0:
        raise ForwardRecoveryError("Persistence still has unexpired Session writer leases")
    runtime_update._verify_live_network_membership(candidate_compose, live, docker_prefix)
    edge = runtime_update._current_edge_status(
        docker_prefix, journal["edge"]["containerId"], timeout_seconds=15
    )
    runtime_update._verify_edge_dead_count(
        journal["edge"], edge["dead_count"], "forward-recovery gate"
    )
    if (
        edge.get("paused") is not True
        or edge.get("revision") != journal["edge"].get("pauseRevision")
        or journal["edge"].get("pauseHolder") != journal["operationId"]
    ):
        raise ForwardRecoveryError("Teams ingress is not held by this exact operation")
    nav = runtime_update._current_navigator_status(
        docker_prefix, journal["navigator"]["initialContainerId"], timeout_seconds=15
    )
    drain_attempt = journal["navigator"].get("drainAttempt")
    expected_attempt = {
        "containerId": journal["navigator"].get("initialContainerId"),
        "instanceId": journal["navigator"].get("initialInstanceId"),
        "initialGeneration": journal["navigator"].get("initialGeneration"),
        "generation": journal["navigator"].get("drainGeneration"),
    }
    notifications = nav.get("notifications", {})
    dispatches = nav.get("dispatches", {})
    drained_counts = (
        nav.get("countsComplete") is True
        and nav.get("activeTasks") == 0
        and isinstance(dispatches, dict)
        and dispatches.get("active") == 0
        and dispatches.get("waiting") == 0
        and isinstance(notifications, dict)
        and notifications.get("inFlight") is False
    )
    draining_exact = (
        nav.get("mode") == "draining"
        and nav.get("token") is not None
        and nav.get("startedAt") is not None
        and notifications.get("pollingPaused") is True
        and nav.get("activeWorkDrained") is True
    )
    if (
        drain_attempt != expected_attempt
        or nav.get("instanceId") != journal["navigator"].get("initialInstanceId")
        or nav.get("generation") != journal["navigator"].get("drainGeneration")
        or not drained_counts
        or not draining_exact
        or journal["navigator"].get("drained") is not True
    ):
        raise ForwardRecoveryError("the exact original Navigator drain is not held")
    readiness = journal.get("exchangeReadiness", {})
    if readiness.get("required") is not True or readiness.get("verified") is not True:
        raise ForwardRecoveryError("the operation's required Exchange readiness proof is missing")
    return candidate_compose, candidate_lock, live


def _build_preview(
    journal_dir: Path, operation_id: str
) -> tuple[dict[str, Any], Path, Path, Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    plan, journal, plan_path, journal_path = _load_context(journal_dir, operation_id)
    docker_prefix = plan["policy"]["dockerPrefix"]
    runtime_update._validate_docker_prefix(docker_prefix)
    stage_transition._verify_all_inputs(plan)
    stage_transition._check_selection_unchanged(plan)
    source_module_path = Path(__file__).resolve(strict=True)
    _private_regular(plan_path, "sealed stage transition plan")
    candidate_image = _candidate_image(plan, docker_prefix)
    before, expected, second, source_hashes = _candidate_source_replay(
        plan=plan, journal=journal, image=candidate_image, docker_prefix=docker_prefix
    )
    if _candidate_image(plan, docker_prefix) != candidate_image:
        raise ForwardRecoveryError("candidate image identity changed during isolated source replay")
    candidate_compose, candidate_lock, live_services = _mixed_runtime_readback(
        plan=plan, journal=journal, docker_prefix=docker_prefix
    )
    persistence_container = journal["targetProgress"][TARGET]["healthyContainerId"]
    live_db = stage_transition._live_sqlite_report(
        docker_prefix=docker_prefix,
        container_id=persistence_container,
        database_path=plan["database"]["containerPath"],
        comparison_contract=stage_transition._sqlite_comparison_contract(
            journal["database"]["snapshot"]
        ),
    )
    _compare_reports(expected, live_db, "live Persistence database")
    if live_db["integrity"] != "ok":
        raise ForwardRecoveryError("live Persistence SQLite integrity_check is not ok")
    selector_path = Path(journal["selection"]["path"])
    selector_sha = stage_transition._current_selection_digest(selector_path)
    backup_sha = _sha256_file(Path(journal["database"]["backupPath"]))
    preview: dict[str, Any] = {
        "schemaVersion": PREVIEW_SCHEMA,
        "operationId": operation_id,
        "planPath": str(plan_path),
        "planFileSha256": _sha256_file(plan_path),
        "planSha256": plan["planSha256"],
        "journalPath": str(journal_path),
        "journalSha256": _sha256_file(journal_path),
        "selectorPath": str(selector_path),
        "selectorSha256": selector_sha,
        "backupPath": str(plan["database"]["backupPath"]),
        "backupSha256": backup_sha,
        "candidateImage": candidate_image,
        "navigatorSourceCommit": SUPPORTED_NAVIGATOR_COMMIT,
        "sourceFiles": SUPPORTED_SOURCE_FILES,
        "sourceFilesVerifiedInCandidateImage": source_hashes,
        "recoveryModuleSha256": _sha256_file(source_module_path),
        "replay": {
            "network": "none",
            "secretsMounted": False,
            "databaseMounted": "independent-private-copy",
            "startupSequence": ["PersistenceStore", "WorkStore"],
            "secondPassIdempotent": _report_projection(expected) == _report_projection(second),
            "before": before,
            "expected": expected,
            "secondPass": second,
        },
        "liveDatabase": live_db,
        "liveServiceIdsSha256": _sha256_bytes(
            _canonical_json(
                {name: row["containerId"] for name, row in sorted(live_services.items())}
            )
        ),
        "createdAtUtc": datetime.now(UTC).isoformat(),
    }
    if preview["replay"]["secondPassIdempotent"] is not True:
        raise ForwardRecoveryError("candidate migration is not idempotent on the private copy")
    preview["stateSha256"] = _preview_state_sha256(preview)
    preview["previewSha256"] = _sha256_bytes(_canonical_json(preview))
    return (
        preview,
        plan_path,
        journal_path,
        source_module_path,
        plan,
        journal,
        {"compose": candidate_compose, "lock": candidate_lock},
    )


def _preview_state_sha256(preview: dict[str, Any]) -> str:
    """Hash every recovery binding while excluding creation time and digest fields."""

    return _sha256_bytes(
        _canonical_json(
            {
                key: value
                for key, value in preview.items()
                if key not in {"createdAtUtc", "previewSha256", "stateSha256"}
            }
        )
    )


def _expected_receipt_path(plan_path: Path, operation_id: str) -> Path:
    """Return the single receipt path that prevents alternate-name reauthorization."""

    return plan_path.parent / f"{operation_id}.persistence-forward-recovery-receipt.json"


def _validate_receipt_path(receipt_path: Path, plan_path: Path, operation_id: str) -> None:
    if receipt_path != _expected_receipt_path(plan_path, operation_id):
        raise ForwardRecoveryError(
            "authorization receipt path must be deterministic for this operation"
        )


def prepare_forward_recovery(
    operation_id: str, *, journal_dir: Path, preview_path: Path
) -> dict[str, Any]:
    """Create a read-only proof for one exact migration failure; no live mutation occurs."""

    _private_directory(preview_path.parent, "recovery preview directory")
    plan, _, _, _ = _load_context(journal_dir, operation_id)
    with runtime_update._operation_lock(plan["project"]):
        preview, plan_path, *_ = _build_preview(journal_dir, operation_id)
        if preview_path.parent != plan_path.parent:
            raise ForwardRecoveryError(
                "preview must be stored beside the exact sealed operation plan"
            )
        written_sha = _write_exclusive(preview_path, preview)
    return {
        "operationId": operation_id,
        "state": "PREPARED_READ_ONLY",
        "previewPath": str(preview_path),
        "previewFileSha256": written_sha,
        "previewSha256": preview["previewSha256"],
        "expectedSchemaSha256": preview["replay"]["expected"]["schemaSha256"],
        "liveSchemaSha256": preview["liveDatabase"]["schemaSha256"],
    }


def _verify_preview_file(path: Path, expected: str) -> dict[str, Any]:
    preview = _read_json(path, "forward-recovery preview")
    if (
        preview.get("schemaVersion") != PREVIEW_SCHEMA
        or preview.get("previewSha256") != expected
        or _sha256_bytes(
            _canonical_json({k: v for k, v in preview.items() if k != "previewSha256"})
        )
        != expected
        or preview.get("stateSha256") != _preview_state_sha256(preview)
    ):
        raise ForwardRecoveryError(
            "operator confirmation does not match the exact recovery preview"
        )
    return preview


def commit_forward_recovery(
    operation_id: str,
    *,
    journal_dir: Path,
    preview_path: Path,
    confirm_preview_sha256: str,
    receipt_path: Path,
) -> dict[str, Any]:
    """Lock the exact operation while authorizing and completing forward recovery."""

    plan, _, _, _ = _load_context(journal_dir, operation_id)
    with runtime_update._operation_lock(plan["project"]):
        return _commit_forward_recovery_locked(
            operation_id,
            journal_dir=journal_dir,
            preview_path=preview_path,
            confirm_preview_sha256=confirm_preview_sha256,
            receipt_path=receipt_path,
        )


def _commit_forward_recovery_locked(
    operation_id: str,
    *,
    journal_dir: Path,
    preview_path: Path,
    confirm_preview_sha256: str,
    receipt_path: Path,
) -> dict[str, Any]:
    """Authorize and finish the verified forward-only post-Persistence cutover tail."""

    if stage_transition.SHA256_RE.fullmatch(confirm_preview_sha256) is None:
        raise ForwardRecoveryError("operator confirmation must be a SHA-256 preview digest")
    preview = _verify_preview_file(preview_path, confirm_preview_sha256)
    if preview.get("operationId") != operation_id:
        raise ForwardRecoveryError("preview belongs to a different operation")
    _private_directory(receipt_path.parent, "recovery receipt directory")
    fresh, plan_path, journal_path, source_path, plan, journal, runtime = _build_preview(
        journal_dir, operation_id
    )
    if preview_path.parent != plan_path.parent or receipt_path.parent != plan_path.parent:
        raise ForwardRecoveryError(
            "preview and receipt must stay beside the exact sealed operation plan"
        )
    _validate_receipt_path(receipt_path, plan_path, operation_id)
    if fresh["stateSha256"] != preview.get("stateSha256"):
        raise ForwardRecoveryError("live state changed after the read-only recovery preview")
    if receipt_path.exists() or receipt_path.is_symlink():
        raise ForwardRecoveryError("forward-recovery authorization receipt already exists")
    authorization: dict[str, Any] = {
        "schemaVersion": RECOVERY_SCHEMA,
        "operationId": operation_id,
        "authorizedAtUtc": datetime.now(UTC).isoformat(),
        "authorization": "FORWARD_COMMIT_ONLY_AFTER_EXACT_SOURCE_REPLAY",
        "confirmedPreviewSha256": confirm_preview_sha256,
        "previewPath": str(preview_path),
        "previewFileSha256": _sha256_file(preview_path),
        "planSha256": plan["planSha256"],
        "journalSha256BeforeRecovery": _sha256_file(journal_path),
        "selectorSha256BeforeRecovery": fresh["selectorSha256"],
        "backupSha256": fresh["backupSha256"],
        "navigatorSourceCommit": SUPPORTED_NAVIGATOR_COMMIT,
        "sourceFiles": SUPPORTED_SOURCE_FILES,
        "candidateImage": fresh["candidateImage"],
        "expectedSchemaSha256": fresh["replay"]["expected"]["schemaSha256"],
        "liveSchemaSha256": fresh["liveDatabase"]["schemaSha256"],
        "recoveryModuleSha256": _sha256_file(source_path),
    }
    authorization["receiptSha256"] = _sha256_bytes(_canonical_json(authorization))
    receipt_file_sha = _write_exclusive(receipt_path, authorization)
    journal["persistenceForwardRecovery"] = {
        "contractClass": "SOURCE_PINNED_FORWARD_MIGRATION",
        "state": "AUTHORIZED",
        "receiptPath": str(receipt_path),
        "receiptSha256": authorization["receiptSha256"],
        "receiptFileSha256": receipt_file_sha,
        "previewSha256": confirm_preview_sha256,
        "expectedSchemaSha256": authorization["expectedSchemaSha256"],
        "sourceCommit": SUPPORTED_NAVIGATOR_COMMIT,
        "candidateImageId": fresh["candidateImage"]["imageId"],
    }
    journal["database"]["postMigration"] = {
        **fresh["liveDatabase"],
        "migrationCompatibility": {
            "class": "SOURCE_PINNED_FORWARD_MIGRATION",
            "sourceCommit": SUPPORTED_NAVIGATOR_COMMIT,
            "schemaSha256": authorization["expectedSchemaSha256"],
            "tableCount": len(fresh["liveDatabase"]["tables"]),
        },
    }
    journal["database"]["restorePerformed"] = False
    stage_transition._write_journal(
        journal_path, journal, "PERSISTENCE_FORWARD_RECOVERY_AUTHORIZED", state="RUNNING"
    )
    try:
        mixed_compose = runtime["compose"]
        live = stage_transition._live_service_inventory(
            compose=mixed_compose,
            image_lock=runtime["lock"],
            docker_prefix=plan["policy"]["dockerPrefix"],
            mount_identity_by_service=plan["runtimeConfigIdentity"]["active"],
        )
        runtime_update._verify_live_network_membership(
            mixed_compose, live, plan["policy"]["dockerPrefix"]
        )
        stage_transition._resume_executor_after_transition(
            plan=plan,
            journal=journal,
            candidate_compose=mixed_compose,
            docker_prefix=plan["policy"]["dockerPrefix"],
        )
        journal["resourceReadback"] = stage_transition._validate_post_update_capacity(
            compose=mixed_compose,
            services=live,
            docker_prefix=plan["policy"]["dockerPrefix"],
            minimum_available_mib=plan["candidatePreflight"]["minimumMemAvailableMiB"],
        )
        stage_transition._write_journal(journal_path, journal, "RESOURCE_READBACK_VERIFIED")
        selection = stage_transition._promote_selection(
            plan=plan, journal=journal, journal_path=journal_path
        )
        stage_transition._resume_ingress_after_safe_state(
            journal=journal, journal_path=journal_path, docker_prefix=plan["policy"]["dockerPrefix"]
        )
        delivery = stage_transition._verify_final_delivery_readback(
            journal=journal, docker_prefix=plan["policy"]["dockerPrefix"]
        )
        journal["edge"]["latestDeadCount"] = delivery["dead_count"]
        journal["persistenceForwardRecovery"]["state"] = "APPLIED"
        stage_transition._write_journal(journal_path, journal, "APPLIED", state="APPLIED")
        return {
            **stage_transition._journal_summary(journal),
            "persistenceSameSchemaStatus": "NOT_APPLICABLE_FORWARD_MIGRATION",
            "persistenceMigrationStatus": "SOURCE_PINNED_FORWARD_MIGRATION_APPLIED",
            "activeRuntimeSelection": stage_transition._selection_summary(selection),
            "forwardRecoveryReceiptSha256": authorization["receiptSha256"],
        }
    except (
        OSError,
        ForwardRecoveryError,
        stage_transition.StageTransitionError,
        runtime_update.RuntimeUpdateError,
        subprocess.SubprocessError,
    ) as error:
        journal["persistenceForwardRecovery"]["state"] = "RECOVERY_REQUIRED"
        journal["lastErrorCode"] = "persistence_forward_recovery_readback_required"
        stage_transition._write_journal(
            journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED"
        )
        raise ForwardRecoveryError(
            "forward recovery stopped; inspect the exact journal/selector/edge state before any continuation"
        ) from error


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare", help="read-only exact-source replay and live validation"
    )
    prepare.add_argument("--operation-id", required=True)
    prepare.add_argument("--journal-dir", type=Path, required=True)
    prepare.add_argument("--preview-file", type=Path, required=True)
    commit = commands.add_parser("commit", help="explicitly authorize forward-only recovery")
    commit.add_argument("--operation-id", required=True)
    commit.add_argument("--journal-dir", type=Path, required=True)
    commit.add_argument("--preview-file", type=Path, required=True)
    commit.add_argument("--confirm-preview-sha256", required=True)
    commit.add_argument("--receipt-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            result = prepare_forward_recovery(
                arguments.operation_id,
                journal_dir=arguments.journal_dir.resolve(),
                preview_path=arguments.preview_file.resolve(),
            )
        else:
            result = commit_forward_recovery(
                arguments.operation_id,
                journal_dir=arguments.journal_dir.resolve(),
                preview_path=arguments.preview_file.resolve(),
                confirm_preview_sha256=arguments.confirm_preview_sha256,
                receipt_path=arguments.receipt_file.resolve(),
            )
    except (
        OSError,
        ForwardRecoveryError,
        stage_transition.StageTransitionError,
        runtime_update.RuntimeUpdateError,
        preflight.PreflightError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Persistence forward recovery blocked: {error}", file=sys.stderr)
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
