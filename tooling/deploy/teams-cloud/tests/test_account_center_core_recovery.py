"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_account_center_core_recovery.py                                 │
│  Module: tooling.deploy.teams_cloud.tests.test_account_center_core_recovery│
│  Role: Verify the one-time Account Center core-restore safety boundary.  │
│                                                                          │
│  模块职责：验证账户中心核心恢复的数据库保护、容器约束与防重放行为。    │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
recovery = importlib.import_module("account_center_core_recovery")


class AccountCenterCoreRecoveryTests(unittest.TestCase):
    """Exercise read-only evidence checks and one-shot recovery behavior."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.root.chmod(0o700)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _database(self, path: Path) -> None:
        """Create a small v8 store with five provider identities and secret churn."""

        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE provider_profiles ("
            "profile_ref TEXT PRIMARY KEY, anchor_issuer TEXT, anchor_subject TEXT, "
            "organization_id TEXT, workspace_id TEXT, provider TEXT, identity_ref TEXT, "
            "target_scope_json TEXT, external_subject TEXT, status TEXT, auth_mode TEXT, "
            "access_token_ciphertext BLOB)"
        )
        connection.execute(
            "CREATE TABLE provider_credential_leases (lease_id TEXT PRIMARY KEY, token BLOB)"
        )
        connection.execute("PRAGMA user_version=8")
        providers = ("azure", "gcp", "github", "huggingface", "runpod")
        connection.executemany(
            "INSERT INTO provider_profiles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f"profile-{provider}",
                    "issuer",
                    "subject",
                    "org",
                    "workspace",
                    provider,
                    f"identity-{provider}",
                    '{"scope":"read"}',
                    f"external-{provider}",
                    "healthy",
                    "delegated_oauth",
                    f"encrypted-{provider}".encode(),
                )
                for provider in providers
            ],
        )
        connection.execute("INSERT INTO provider_credential_leases VALUES ('lease-1', X'01')")
        connection.commit()
        connection.close()

    def _context_fixture(self) -> tuple[dict, dict, dict, dict, dict, dict]:
        """Build exact private input records and mock only external Docker reads."""

        case_root = self.root / f"case-{uuid.uuid4()}"
        case_root.mkdir(mode=0o700)
        database_path = case_root / "data" / "accounts.sqlite3"
        database_path.parent.mkdir(mode=0o700)
        self._database(database_path)
        database = recovery._database(database_path)
        snapshot_path = case_root / "snapshot.sqlite3"
        snapshot_path.write_bytes(b"immutable v8 snapshot")
        snapshot_sha = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()

        source = {
            "phase": "microsoft-graph-provider-bootstrap",
            "operationId": str(uuid.uuid4()),
            "project": "cyrene-teams-cloud",
            "targets": ["navigator-persistence", "account-center"],
            "journalDir": str(case_root),
            "activeServices": {
                "account-center": {"containerId": "a" * 64, "imageId": "sha256:" + "1" * 64},
                "navigator-persistence": {"containerId": "b" * 64, "imageId": "sha256:" + "2" * 64},
                "navigator": {"containerId": "c" * 64, "imageId": "sha256:" + "3" * 64},
            },
            "candidateServices": {
                "account-center": {
                    "active": {
                        "imageRef": "registry/account-center:locked",
                        "imageId": "sha256:" + "1" * 64,
                    },
                    "candidate": {"imageId": "sha256:" + "4" * 64},
                }
            },
            "edgeDelivery": {"containerId": "f" * 64},
            "policy": {"dockerPrefix": ["docker"], "healthTimeoutSeconds": 15},
            "planSha256": "e" * 64,
        }
        source_plan_path = case_root / "sourcePlan.json"
        source["planPath"] = str(source_plan_path.resolve())
        journal = {
            "operationId": source["operationId"],
            "planSha256": source["planSha256"],
            "planPath": source["planPath"],
            "project": source["project"],
            "targets": list(source["targets"]),
            "state": "RECOVERY_REQUIRED",
            "lastErrorCode": "account_center_schema_downgrade_unsupported",
            "selection": {"promoted": False, "promotionIntent": False},
            "targetProgress": {
                "account-center": {
                    "phase": "CONTAINER_READBACK",
                    "applyIntent": True,
                    "rollbackIntent": False,
                    "rollbackContainerId": None,
                    "candidateContainerId": "d" * 64,
                    "candidateImageId": "sha256:" + "4" * 64,
                    "oldImageId": "sha256:" + "1" * 64,
                },
                "navigator-persistence": {
                    "phase": "APPLIED",
                    "applyIntent": False,
                    "candidateContainerId": "e" * 64,
                    "healthyContainerId": "e" * 64,
                    "candidateImageId": "sha256:" + "5" * 64,
                },
            },
            "edge": {
                "containerId": "f" * 64,
                "initialRevision": 6,
                "pauseIntent": True,
                "pauseRevision": 7,
                "pauseHolder": source["operationId"],
                "initialDeadCount": 0,
            },
            "navigator": {
                "initialContainerId": "c" * 64,
                "initialInstanceId": "navigator-instance",
                "initialGeneration": 4,
            },
        }
        evidence = {
            "state": "ACCOUNT_CENTER_READONLY_DATABASE_RECONCILED",
            "databasePath": str(database_path),
            "userVersion": 8,
            "schemaSha256": database["schemaSha256"],
            "tables": database["tables"],
            "providers": database["providers"],
            "foreignKeyViolationCount": 0,
            "quickCheck": ["ok"],
            "snapshotPath": str(snapshot_path),
            "snapshotSha256": snapshot_sha,
            "oldImageId": "sha256:" + "1" * 64,
            "mutationsToLiveDatabase": False,
        }
        compatibility = {
            "state": "OLD_ACCOUNT_CENTER_DATABASE_COMPATIBLE",
            "imageId": evidence["oldImageId"],
            "snapshotSha256": snapshot_sha,
            "oldImageSchemaVersion": 8,
            "schemaSha256": database["schemaSha256"],
            "logicalDatabaseUnchanged": True,
            "foreignKeyViolationCount": 0,
            "tableDigestsMatchLiveReadonlyReceipt": True,
            "liveDatabaseMounted": False,
            "secretsMounted": False,
            "network": "none",
        }
        files = {}
        for key, document in (
            ("sourcePlan", source),
            ("sourceJournal", journal),
            ("databaseEvidence", evidence),
            ("compatibilityEvidence", compatibility),
        ):
            path = source_plan_path if key == "sourcePlan" else case_root / f"{key}.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            path.chmod(0o600)
            files[key] = recovery._file(path)

        identities = {
            "account-center": {"id": "d" * 64, "imageId": "sha256:" + "4" * 64},
            "navigator-persistence": {"id": "e" * 64, "imageId": "sha256:" + "5" * 64},
            "navigator": {"id": "c" * 64, "imageId": "sha256:" + "3" * 64},
        }
        containers = {
            name: {
                "Config": {
                    "Env": (
                        [
                            "CYRENE_PLUGIN_STORAGE_PATH=/data",
                            'CYRENE_CAPABILITY_CONFIGURATION_JSON={"storage_path":"/data/accounts.sqlite3"}',
                        ]
                        if name == "account-center"
                        else []
                    )
                },
                "State": {"Running": True, "Health": {"Status": "healthy"}},
                "Mounts": (
                    [
                        {
                            "Type": "volume",
                            "Destination": "/data",
                            "Source": str(database_path.parent),
                        }
                    ]
                    if name == "account-center"
                    else []
                ),
            }
            for name in identities
        }
        baseline = {
            "containers": identities,
            "databasePath": str(database_path),
            "database": database,
        }
        return (
            files,
            source,
            journal,
            evidence,
            compatibility,
            {"identities": identities, "containers": containers, "baseline": baseline},
        )

    def _context_patches(self, files, source, journal, evidence, compatibility, runtime):
        documents = {
            files["sourcePlan"]["path"]: source,
            files["databaseEvidence"]["path"]: evidence,
            files["compatibilityEvidence"]["path"]: compatibility,
        }
        return (
            mock.patch.object(recovery, "_read", side_effect=lambda path: documents[str(path)]),
            mock.patch.object(recovery.st, "_validate_plan"),
            mock.patch.object(recovery.st, "_verify_all_inputs"),
            mock.patch.object(recovery.st, "_check_selection_unchanged"),
            mock.patch.object(recovery.st, "_read_journal", return_value=journal),
            mock.patch.object(
                recovery, "_inventory", return_value=(runtime["identities"], runtime["containers"])
            ),
            mock.patch.object(
                recovery.subprocess,
                "check_output",
                return_value=json.dumps(
                    [{"Id": source["candidateServices"]["account-center"]["active"]["imageId"]}]
                ),
            ),
            mock.patch.object(recovery, "_holds", return_value={"verified": True}),
        )

    def test_database_read_is_read_only_and_profile_identity_survives_token_refresh(self) -> None:
        """A legal credential refresh may change ciphertext while profile scope stays fixed."""

        database_path = self.root / "accounts.sqlite3"
        self._database(database_path)
        before_bytes = hashlib.sha256(database_path.read_bytes()).hexdigest()
        before = recovery._database(database_path)
        self.assertEqual(before["userVersion"], 8)
        self.assertEqual(before["foreignKeyViolationCount"], 0)
        self.assertEqual(
            before["providers"],
            [
                {"provider": provider, "count": 1}
                for provider in ("azure", "gcp", "github", "huggingface", "runpod")
            ],
        )
        self.assertEqual(hashlib.sha256(database_path.read_bytes()).hexdigest(), before_bytes)

        connection = sqlite3.connect(database_path)
        connection.execute(
            "UPDATE provider_profiles SET access_token_ciphertext=? WHERE provider='github'",
            (b"rotated-ciphertext",),
        )
        connection.commit()
        connection.close()
        refreshed = recovery._database(database_path)

        self.assertEqual(refreshed["profilesIdentitySha256"], before["profilesIdentitySha256"])
        before_tables = {row["table"]: row["rowDigestSha256"] for row in before["tables"]}
        refreshed_tables = {row["table"]: row["rowDigestSha256"] for row in refreshed["tables"]}
        self.assertNotEqual(
            refreshed_tables["provider_profiles"], before_tables["provider_profiles"]
        )
        self.assertEqual(refreshed["foreignKeyViolationCount"], 0)

    def test_context_rejects_a_different_account_center_container_with_same_image(self) -> None:
        """Recovery is bound to the failed operation's exact container, not just its image."""

        files, source, journal, evidence, compatibility, runtime = self._context_fixture()
        runtime["identities"]["account-center"]["id"] = "f" * 64
        patches = self._context_patches(files, source, journal, evidence, compatibility, runtime)
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            with self.assertRaises(recovery.st.StageTransitionError):
                recovery._context(files)

    def test_context_rejects_database_changes_before_recovery(self) -> None:
        """The fresh pre-apply schema and provider records must match sealed evidence."""

        files, source, journal, evidence, compatibility, runtime = self._context_fixture()
        altered = dict(evidence)
        altered["providers"] = [{"provider": "github", "count": 5}]
        patches = self._context_patches(files, source, journal, altered, compatibility, runtime)
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            with self.assertRaisesRegex(
                recovery.st.StageTransitionError, "differs from the read-only evidence"
            ):
                recovery._context(files)

    def test_context_rejects_unbound_journal_progress_and_compatibility_proofs(self) -> None:
        """Every recovery gate is tied to the exact failed plan and tested old image."""

        cases = (
            ("journal", "planSha256", "d" * 64),
            ("journal", "planPath", str(self.root / "other-plan.json")),
            ("journal", "project", "another-compose-project"),
            ("journal", "targets", ["account-center"]),
            ("journal-account", "phase", "APPLIED"),
            ("journal-account", "rollbackIntent", True),
            ("journal-persistence", "applyIntent", True),
            ("journal-persistence", "healthyContainerId", "f" * 64),
            ("compatibility", "foreignKeyViolationCount", 1),
            ("compatibility", "tableDigestsMatchLiveReadonlyReceipt", False),
            ("compatibility", "liveDatabaseMounted", True),
            ("compatibility", "secretsMounted", True),
            ("compatibility", "network", "bridge"),
        )
        for record, field, value in cases:
            with self.subTest(record=record, field=field):
                files, source, journal, evidence, compatibility, runtime = self._context_fixture()
                if record == "journal":
                    journal[field] = value
                elif record == "journal-account":
                    journal["targetProgress"]["account-center"][field] = value
                elif record == "journal-persistence":
                    journal["targetProgress"]["navigator-persistence"][field] = value
                else:
                    compatibility[field] = value

                changed_record = journal if record.startswith("journal") else compatibility
                changed_key = (
                    "sourceJournal" if record.startswith("journal") else "compatibilityEvidence"
                )
                changed_path = Path(files[changed_key]["path"])
                changed_path.write_text(json.dumps(changed_record), encoding="utf-8")
                changed_path.chmod(0o600)
                files[changed_key] = recovery._file(changed_path)
                patches = self._context_patches(
                    files, source, journal, evidence, compatibility, runtime
                )
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    with self.assertRaises(recovery.st.StageTransitionError):
                        recovery._context(files)

    def test_holds_require_the_original_ingress_pause_and_fully_drained_navigator(self) -> None:
        """Do not recover while the original edge lease or Navigator drain has changed."""

        operation_id = str(uuid.uuid4())
        source = {
            "operationId": operation_id,
            "activeServices": {"navigator": {"containerId": "c" * 64}},
            "edgeDelivery": {"containerId": "e" * 64},
        }
        journal = {
            "edge": {
                "containerId": "e" * 64,
                "initialRevision": 6,
                "pauseIntent": True,
                "pauseRevision": 7,
                "pauseHolder": operation_id,
                "initialDeadCount": 2,
            },
            "navigator": {
                "initialContainerId": "c" * 64,
                "initialInstanceId": "navigator-instance",
                "initialGeneration": 7,
            },
        }
        edge = {"paused": True, "revision": 7, "dead_count": 2}
        nav = {
            "mode": "draining",
            "instanceId": "navigator-instance",
            "generation": 8,
            "activeWorkDrained": True,
            "activeTasks": 0,
            "dispatches": {"active": 0, "waiting": 0},
            "notifications": {"pollingPaused": True, "inFlight": False},
        }
        with (
            mock.patch.object(
                recovery.st.runtime_update, "_current_edge_status", return_value=edge
            ),
            mock.patch.object(
                recovery.st.runtime_update, "_current_navigator_status", return_value=nav
            ),
        ):
            recovery._holds(source, journal, ["docker"])
            bad_holder = copy.deepcopy(journal)
            bad_holder["edge"]["pauseHolder"] = str(uuid.uuid4())
            with self.assertRaises(recovery.st.StageTransitionError):
                recovery._holds(source, bad_holder, ["docker"])
            for field, value in (
                ("activeTasks", 1),
                ("dispatches", {"active": 0, "waiting": 1}),
                ("notifications", {"pollingPaused": True, "inFlight": True}),
            ):
                with self.subTest(field=field):
                    changed = copy.deepcopy(nav)
                    changed[field] = value
                    with (
                        mock.patch.object(
                            recovery.st.runtime_update,
                            "_current_navigator_status",
                            return_value=changed,
                        ),
                        self.assertRaises(recovery.st.StageTransitionError),
                    ):
                        recovery._holds(source, journal, ["docker"])

    def test_apply_refuses_replay_when_the_recovery_journal_already_exists(self) -> None:
        """An existing one-shot journal blocks before context checks or Docker calls."""

        operation_id = str(uuid.uuid4())
        plan = {
            "schemaVersion": recovery.SCHEMA,
            "operationId": operation_id,
            "project": "cyrene-teams-cloud",
            "resultState": recovery.RESTORED,
            "sourceOperationId": str(uuid.uuid4()),
            "implementationSha256": recovery._implementation(),
            "inputs": {},
            "baseline": {},
        }
        plan["planSha256"] = recovery._digest(plan)
        plan_path = self.root / "recovery.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        plan_path.chmod(0o600)
        journal_path = self.root / f"{operation_id}.journal.json"
        journal_path.write_text('{"state":"RECOVERY_REQUIRED"}', encoding="utf-8")
        journal_path.chmod(0o600)

        with (
            mock.patch.object(recovery.st.runtime_update, "_require_update_operator"),
            mock.patch.object(
                recovery.st.runtime_update, "_operation_lock", return_value=contextlib.nullcontext()
            ),
            mock.patch.object(recovery, "_read", return_value=plan),
            mock.patch.object(
                recovery, "_context", side_effect=AssertionError("must not reconcile again")
            ),
            mock.patch.object(
                recovery.st,
                "_compose_target_update",
                side_effect=AssertionError("must not replay Docker"),
            ),
            self.assertRaisesRegex(recovery.st.StageTransitionError, "do not replay"),
        ):
            recovery.apply(plan_path, plan["planSha256"])

        self.assertEqual(
            json.loads(journal_path.read_text(encoding="utf-8"))["state"], "RECOVERY_REQUIRED"
        )

    def test_apply_restores_only_account_center_and_preserves_persistence(self) -> None:
        """Restore the pinned AC image once and leave the applied Persistence untouched."""

        files, source, original, _evidence, _compatibility, runtime = self._context_fixture()
        case_root = Path(files["sourcePlan"]["path"]).parent
        original_bytes = Path(files["sourceJournal"]["path"]).read_bytes()
        recovery_plan = {
            "schemaVersion": recovery.SCHEMA,
            "operationId": str(uuid.uuid4()),
            "project": source["project"],
            "sourceOperationId": source["operationId"],
            "createdAtUtc": "2026-10-30T12:00:00+00:00",
            "implementationSha256": recovery._implementation(),
            "inputs": files,
            "baseline": runtime["baseline"],
            "resultState": recovery.RESTORED,
        }
        recovery_plan["planSha256"] = recovery._digest(recovery_plan)
        recovery_path = case_root / "one-time-recovery-plan.json"
        recovery_path.write_text(json.dumps(recovery_plan), encoding="utf-8")
        recovery_path.chmod(0o600)

        post_identities = copy.deepcopy(runtime["identities"])
        post_identities["account-center"] = {
            "id": "f" * 64,
            "imageId": source["candidateServices"]["account-center"]["active"]["imageId"],
        }
        post_containers = copy.deepcopy(runtime["containers"])
        database_after_restore = copy.deepcopy(runtime["baseline"]["database"])

        compose_calls = []
        completed = subprocess.CompletedProcess(
            args=["docker", "compose"], returncode=0, stdout="", stderr=""
        )
        with (
            mock.patch.object(recovery.st.runtime_update, "_require_update_operator"),
            mock.patch.object(
                recovery.st.runtime_update, "_operation_lock", return_value=contextlib.nullcontext()
            ),
            mock.patch.object(recovery, "_read", return_value=recovery_plan),
            mock.patch.object(
                recovery, "_implementation", return_value=recovery_plan["implementationSha256"]
            ),
            mock.patch.object(
                recovery, "_context", return_value=(source, original, runtime["baseline"])
            ),
            mock.patch.object(recovery, "_file", return_value=files["sourceJournal"]),
            mock.patch.object(
                recovery.st,
                "_compose_target_update",
                side_effect=lambda **kwargs: compose_calls.append(kwargs) or completed,
            ),
            mock.patch.object(
                recovery.st,
                "_wait_for_target_service",
                return_value={
                    "containerId": "f" * 64,
                    "imageId": source["candidateServices"]["account-center"]["active"]["imageId"],
                    "health": "healthy",
                },
            ),
            mock.patch.object(
                recovery, "_inventory", return_value=(post_identities, post_containers)
            ),
            mock.patch.object(recovery, "_database", return_value=database_after_restore),
            mock.patch.object(recovery.st, "_check_selection_unchanged"),
            mock.patch.object(recovery.st, "_resume_old_executor_if_needed"),
            mock.patch.object(recovery.st, "_resume_ingress_after_safe_state"),
            mock.patch.object(
                recovery.st,
                "_verify_final_delivery_readback",
                return_value={"paused": False, "dead_count": 0},
            ),
        ):
            result = recovery.apply(recovery_path, recovery_plan["planSha256"])

        self.assertEqual(result["state"], recovery.RESTORED)
        self.assertEqual(len(compose_calls), 1)
        self.assertEqual(compose_calls[0]["service"], "account-center")
        self.assertIs(compose_calls[0]["use_candidate"], False)
        self.assertEqual(
            result["coreRecovery"]["retainedPersistence"],
            runtime["identities"]["navigator-persistence"],
        )
        self.assertFalse(result["coreRecovery"]["databaseRestorePerformed"])
        self.assertEqual(Path(files["sourceJournal"]["path"]).read_bytes(), original_bytes)
        receipt = json.loads(Path(result["journalPath"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["state"], recovery.RESTORED)

    def test_apply_keeps_holds_when_restored_account_center_uses_another_volume(self) -> None:
        """A healthy old image cannot release Teams delivery against a different database."""

        files, source, original, _evidence, _compatibility, runtime = self._context_fixture()
        case_root = Path(files["sourcePlan"]["path"]).parent
        original_bytes = Path(files["sourceJournal"]["path"]).read_bytes()
        recovery_plan = {
            "schemaVersion": recovery.SCHEMA,
            "operationId": str(uuid.uuid4()),
            "project": source["project"],
            "sourceOperationId": source["operationId"],
            "createdAtUtc": "2026-10-30T12:00:00+00:00",
            "implementationSha256": recovery._implementation(),
            "inputs": files,
            "baseline": runtime["baseline"],
            "resultState": recovery.RESTORED,
        }
        recovery_plan["planSha256"] = recovery._digest(recovery_plan)
        recovery_path = case_root / "wrong-volume-recovery-plan.json"
        recovery_path.write_text(json.dumps(recovery_plan), encoding="utf-8")
        recovery_path.chmod(0o600)

        post_identities = copy.deepcopy(runtime["identities"])
        post_identities["account-center"] = {
            "id": "f" * 64,
            "imageId": source["candidateServices"]["account-center"]["active"]["imageId"],
        }
        post_containers = copy.deepcopy(runtime["containers"])
        post_containers["account-center"]["Mounts"][0]["Source"] = str(
            case_root / "different-state-volume"
        )
        compose_calls = []
        completed = subprocess.CompletedProcess(
            args=["docker", "compose"], returncode=0, stdout="", stderr=""
        )
        resume_navigator = mock.Mock()
        resume_ingress = mock.Mock()
        with (
            mock.patch.object(recovery.st.runtime_update, "_require_update_operator"),
            mock.patch.object(
                recovery.st.runtime_update, "_operation_lock", return_value=contextlib.nullcontext()
            ),
            mock.patch.object(recovery, "_read", return_value=recovery_plan),
            mock.patch.object(
                recovery, "_implementation", return_value=recovery_plan["implementationSha256"]
            ),
            mock.patch.object(
                recovery, "_context", return_value=(source, original, runtime["baseline"])
            ),
            mock.patch.object(recovery, "_file", return_value=files["sourceJournal"]),
            mock.patch.object(
                recovery.st,
                "_compose_target_update",
                side_effect=lambda **kwargs: compose_calls.append(kwargs) or completed,
            ),
            mock.patch.object(
                recovery.st,
                "_wait_for_target_service",
                return_value={
                    "containerId": "f" * 64,
                    "imageId": source["candidateServices"]["account-center"]["active"]["imageId"],
                    "health": "healthy",
                },
            ),
            mock.patch.object(
                recovery, "_inventory", return_value=(post_identities, post_containers)
            ),
            mock.patch.object(
                recovery, "_database", side_effect=AssertionError("wrong database must not be read")
            ),
            mock.patch.object(recovery.st, "_check_selection_unchanged"),
            mock.patch.object(recovery.st, "_resume_old_executor_if_needed", resume_navigator),
            mock.patch.object(recovery.st, "_resume_ingress_after_safe_state", resume_ingress),
            self.assertRaisesRegex(
                recovery.st.StageTransitionError, "mounted a different database"
            ),
        ):
            recovery.apply(recovery_path, recovery_plan["planSha256"])

        self.assertEqual(len(compose_calls), 1)
        self.assertFalse(resume_navigator.called)
        self.assertFalse(resume_ingress.called)
        self.assertEqual(Path(files["sourceJournal"]["path"]).read_bytes(), original_bytes)
        receipt_path = case_root / f"{recovery_plan['operationId']}.journal.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["state"], "RECOVERY_REQUIRED")


if __name__ == "__main__":
    unittest.main()
