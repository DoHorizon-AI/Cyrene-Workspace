"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_persistence_forward_recovery.py                                 │
│  Module: tooling.deploy.teams_cloud.tests.test_persistence_forward_recovery│
│  Role: Verify the narrowly scoped Persistence forward-recovery contract. │
│                                                                          │
│  模块职责：验证受限的持久化向前恢复契约与失败关闭行为。                  │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
recovery = importlib.import_module("persistence_forward_recovery")


def _sqlite_report(*, preserved: bool = False) -> dict[str, Any]:
    """Build a closed, valid one-table SQLite inventory for contract tests."""

    schema_objects = [
        {"type": "table", "name": "sessions", "sql": "CREATE TABLE sessions (id TEXT)"}
    ]
    table: dict[str, Any] = {
        "columns": [
            {
                "name": "id",
                "type": "TEXT",
                "notNull": False,
                "default": None,
                "primaryKeyOrdinal": 0,
                "hidden": 0,
            }
        ],
        "createSql": schema_objects[0]["sql"],
        "primaryKeyColumns": [],
        "foreignKeys": [],
        "checkConstraints": [],
        "uniqueIndexes": [],
        "rowCount": 0,
        "rowsDigest": hashlib.sha256(b"").hexdigest(),
    }
    if preserved:
        table["preservedRowsCount"] = 0
        table["preservedRowsDigest"] = hashlib.sha256(b"").hexdigest()
    return {
        "integrity": "ok",
        "userVersion": 0,
        "schemaSha256": hashlib.sha256(recovery._canonical_json(schema_objects)).hexdigest(),
        "objects": ["table:sessions"],
        "schemaObjects": schema_objects,
        "tables": {"sessions": table},
    }


def _supported_failure() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the exact one-target migration-gate failure accepted by this API."""

    plan = {
        "imageUpdateTarget": recovery.TARGET,
        "phase": "issued-connector-activation",
        "targets": [recovery.TARGET],
    }
    journal = {
        "state": "RECOVERY_REQUIRED",
        "lastErrorCode": recovery.SUPPORTED_ERROR,
        "database": {
            "migrationGateFailed": True,
            "restorePerformed": False,
            "backupPath": "/private/backup.sqlite3",
            "backupSha256": "a" * 64,
            "snapshot": _sqlite_report(),
        },
        "targetProgress": {
            recovery.TARGET: {
                "phase": "APPLIED",
                "applyIntent": False,
                "rollbackIntent": False,
                "candidateContainerId": "1" * 64,
                "healthyContainerId": "1" * 64,
            }
        },
        "selection": {"promoted": False, "promotionIntent": False},
    }
    return plan, journal


def test_only_the_single_persistence_migration_gate_failure_is_eligible() -> None:
    """Reject every recovery class except the witnessed one-target database gate."""

    plan, journal = _supported_failure()
    recovery._validate_supported_failure(plan, journal)

    for mutation in (
        ("state", "RUNNING"),
        ("lastErrorCode", "docker_mutation_outcome_unknown"),
        ("imageUpdateTarget", "exchange"),
    ):
        bad_plan, bad_journal = dict(plan), json.loads(json.dumps(journal))
        key, value = mutation
        if key == "imageUpdateTarget":
            bad_plan[key] = value
        else:
            bad_journal[key] = value
        with pytest.raises(recovery.ForwardRecoveryError):
            recovery._validate_supported_failure(bad_plan, bad_journal)

    bad_journal = json.loads(json.dumps(journal))
    bad_journal["targetProgress"][recovery.TARGET]["applyIntent"] = True
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_supported_failure(plan, bad_journal)

    bad_journal = json.loads(json.dumps(journal))
    bad_journal["persistenceForwardRecovery"] = {"state": "AUTHORIZED"}
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_supported_failure(plan, bad_journal)


def test_baseline_comparison_ignores_only_contract_derived_old_column_digests() -> None:
    """Allow report-only reconciliation fields while preserving strict base data checks."""

    original = _sqlite_report()
    replayed = _sqlite_report(preserved=True)
    assert recovery._report_projection_without_reconciliation(
        original
    ) == recovery._report_projection_without_reconciliation(replayed)

    replayed["tables"]["sessions"]["rowsDigest"] = "b" * 64
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._compare_reports(original, replayed, "test baseline")


def test_expected_and_live_reports_require_exact_schema_and_every_table_digest() -> None:
    """A source replay cannot authorize an unanticipated schema or row difference."""

    expected = _sqlite_report(preserved=True)
    actual = json.loads(json.dumps(expected))
    recovery._compare_reports(expected, actual, "matching live database")

    actual["tables"]["sessions"]["rowCount"] = 1
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._compare_reports(expected, actual, "changed live database")

    actual = json.loads(json.dumps(expected))
    actual_sql = "CREATE TABLE sessions (id INTEGER)"
    actual["schemaObjects"][0]["sql"] = actual_sql
    actual["tables"]["sessions"]["createSql"] = actual_sql
    actual["schemaSha256"] = hashlib.sha256(
        recovery._canonical_json(actual["schemaObjects"])
    ).hexdigest()
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._compare_reports(expected, actual, "changed schema")


def test_preview_state_fingerprint_is_stable_across_prepare_and_commit_time() -> None:
    """Operator confirmation binds runtime evidence, not the prepare timestamp."""

    first = {"operationId": "op", "selectorSha256": "a" * 64, "createdAtUtc": "t1"}
    second = {**first, "createdAtUtc": "t2"}
    assert recovery._preview_state_sha256(first) == recovery._preview_state_sha256(second)
    second["selectorSha256"] = "b" * 64
    assert recovery._preview_state_sha256(first) != recovery._preview_state_sha256(second)


def test_authorization_receipt_path_cannot_be_changed_to_bypass_one_shot_guard(
    tmp_path: Path,
) -> None:
    """One operation has one immutable receipt pathname even if journal write is interrupted."""

    operation_id = "f352340a-eff3-4b13-b047-eb87ad7821e5"
    plan_path = tmp_path / "plan.json"
    expected = recovery._expected_receipt_path(plan_path, operation_id)
    recovery._validate_receipt_path(expected, plan_path, operation_id)
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._validate_receipt_path(
            tmp_path / "alternate-receipt.json", plan_path, operation_id
        )


def test_preview_digest_rejects_tampered_recovery_bindings(tmp_path: Path) -> None:
    """The explicit commit confirmation is tied to the complete immutable preview."""

    preview: dict[str, Any] = {
        "schemaVersion": recovery.PREVIEW_SCHEMA,
        "operationId": "op",
        "createdAtUtc": "2026-10-29T00:00:00+00:00",
        "selectorSha256": "a" * 64,
    }
    preview["stateSha256"] = recovery._preview_state_sha256(preview)
    preview["previewSha256"] = recovery._sha256_bytes(recovery._canonical_json(preview))
    path = tmp_path / "preview.json"
    path.write_bytes(recovery._canonical_json(preview) + b"\n")
    path.chmod(0o600)
    recovery._verify_preview_file(path, preview["previewSha256"])

    preview["selectorSha256"] = "b" * 64
    path.write_bytes(recovery._canonical_json(preview) + b"\n")
    path.chmod(0o600)
    with pytest.raises(recovery.ForwardRecoveryError):
        recovery._verify_preview_file(path, "a" * 64)
