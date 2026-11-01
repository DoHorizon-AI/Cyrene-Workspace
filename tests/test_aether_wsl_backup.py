"""
┌────────────────────────────────────────────────────────────────────────────┐
│ Module: tests.test_aether_wsl_backup                                        │
│ Role: Verify isolated Aether backup, archive, and restore contracts.        │
│                                                                            │
│ 模块职责：验证 Aether 备份、归档和恢复契约。                                 │
└────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts/aether_wsl_backup.py"
SPEC = importlib.util.spec_from_file_location("aether_wsl_backup", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP)

SNAPSHOT_ID = "20261031T220000Z-abcdef123456"
PG_FIXTURE = b"SIMULATED PostgreSQL custom archive fixture; offline test only\n"
BUSINESS_TABLES = (
    "conversations",
    "messages",
    "memories",
    "profiles",
    "graph_nodes",
    "graph_edges",
    "personas",
    "policies",
    "notes",
    "skills",
    "watches",
    "focus_items",
    "handovers",
    "reviews",
    "attachments",
    "tasks",
    "schedules",
    "audit_events",
    "plugin_state",
)


def _private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _database(
    path: Path,
    tables: tuple[str, ...],
    *,
    secret: bool = False,
    secret_value: str | None = None,
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        for table in tables:
            connection.execute(
                f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT NOT NULL)'
            )
            connection.execute(f'INSERT INTO "{table}" (value) VALUES (?)', (f"fixture-{table}",))
        if secret:
            connection.execute(
                "CREATE TABLE plugin_credentials (id INTEGER PRIMARY KEY, access_token TEXT NOT NULL, credential_blob BLOB)"
            )
            if secret_value is not None:
                connection.execute(
                    "INSERT INTO plugin_credentials (access_token) VALUES (?)",
                    (secret_value,),
                )
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o600)


def _exchange_credentials(path: Path) -> None:
    """Adds realistic Exchange verifier metadata without real bearer credentials.

    中文：写入真实 schema 形状的测试 verifier，但不使用真实 bearer credential。
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE api_credentials (
                credential_ref TEXT PRIMARY KEY,
                token_digest TEXT NOT NULL UNIQUE,
                actor_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                organization_id TEXT,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                created_at INTEGER NOT NULL,
                api_key_id TEXT,
                name TEXT,
                expires_at INTEGER,
                revoked_at INTEGER,
                model_scope TEXT,
                resource_version INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        connection.execute(
            "CREATE UNIQUE INDEX ix_api_credentials_api_key_id "
            "ON api_credentials(api_key_id) WHERE api_key_id IS NOT NULL"
        )
        connection.executemany(
            """
            INSERT INTO api_credentials(
                credential_ref, token_digest, actor_id, workspace_id,
                organization_id, enabled, created_at, api_key_id, name,
                expires_at, revoked_at, model_scope, resource_version
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, NULL, ?, 1)
            """,
            [
                (
                    f"cred://exchange/test-{index}",
                    hashlib.sha256(f"synthetic-test-credential-{index}".encode()).hexdigest(),
                    f"actor-{index}",
                    "test-workspace",
                    "test-organization",
                    1_730_000_000_000 + index,
                    f"key-{index}",
                    f"fixture-key-{index}",
                    "responses",
                )
                for index in range(3)
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _catalog(state_root: Path) -> dict[str, Any]:
    return {
        "format": BACKUP.CATALOG_FORMAT,
        "state_root": str(state_root),
        "compose_project": BACKUP.PROJECT_NAME,
        "source_revisions": {
            "navigator": "1" * 40,
            "exchange": "2" * 40,
            "plugins": "3" * 40,
            "workspace": "4" * 40,
        },
        "sources": [
            {
                "id": "navigator-business",
                "owner": "navigator",
                "secret_policy": "external_refs_only",
                "kind": "sqlite",
                "categories": ["navigator_business"],
                "path": "navigator/navigator.sqlite3",
            },
            {
                "id": "navigator-conversation-memory",
                "owner": "navigator",
                "secret_policy": "external_refs_only",
                "kind": "sqlite",
                "categories": ["navigator_business"],
                "path": "navigator/navigator.sqlite3.conversation-memory.sqlite3",
            },
            {
                "id": "navigator-conversation-domains",
                "owner": "navigator",
                "secret_policy": "external_refs_only",
                "kind": "sqlite",
                "categories": ["navigator_business"],
                "path": "navigator/navigator.sqlite3.conversation-domains.sqlite3",
            },
            {
                "id": "dsh-durable-state",
                "owner": "navigator",
                "secret_policy": "external_refs_only",
                "kind": "tree",
                "categories": ["navigator_dsh"],
                "path": "navigator/dsh-home",
            },
            {
                "id": "navigator-attachments",
                "owner": "navigator",
                "secret_policy": "external_refs_only",
                "kind": "tree",
                "categories": ["navigator_attachments"],
                "path": "navigator/navigator.sqlite3.work-attachments",
            },
            {
                "id": "navigator-artifacts",
                "owner": "navigator",
                "secret_policy": "external_refs_only",
                "kind": "tree",
                "categories": ["navigator_attachments"],
                "path": "navigator/artifacts",
            },
            {
                "id": "exchange-state-and-audit",
                "owner": "exchange",
                "secret_policy": BACKUP.EXCHANGE_SECRET_POLICY,
                "kind": "sqlite",
                "categories": ["exchange_database", "exchange_audit"],
                "path": "local-product/exchange-product.sqlite3",
            },
            {
                "id": "plugin-database",
                "owner": "plugins",
                "secret_policy": "external_refs_only",
                "kind": "sqlite",
                "categories": ["plugin_database"],
                "path": "navigator/providers/memory.sqlite3",
            },
            {
                "id": "knowledge-graph-provider-database",
                "owner": "plugins",
                "secret_policy": "external_refs_only",
                "kind": "sqlite",
                "categories": ["plugin_database"],
                "path": "navigator/providers/knowledge-graph.sqlite3",
            },
            {
                "id": "plugin-blobs",
                "owner": "plugins",
                "secret_policy": "external_refs_only",
                "kind": "tree",
                "categories": ["plugin_blobs"],
                "path": "plugins/state/blobs",
            },
            {
                "id": "task-postgres",
                "owner": "workspace",
                "secret_policy": "external_refs_only",
                "kind": "postgres",
                "categories": ["postgres_database"],
                "service": "postgres",
                "database": "cyrene_aether",
            },
            {
                "id": "models-reference",
                "owner": "workspace",
                "secret_policy": "external_refs_only",
                "kind": "reference",
                "categories": ["model_reference"],
                "path": "models/model-reference.json",
            },
        ],
        "external_secret_refs": [
            "env:CYRENE_EXCHANGE_BEARER_TOKEN",
            "cred:cyrene/aether-wsl-product-data",
        ],
        "declared_exclusions": [
            {
                "id": "postgres-secret-env",
                "category": "plaintext_credentials_session_tokens_secret_files",
                "path": "postgres.env",
                "treatment": "metadata_count_only",
            },
            {
                "id": "exchange-service-env",
                "category": "plaintext_credentials_session_tokens_secret_files",
                "path": "local-product/services.env",
                "treatment": "metadata_count_only",
            },
            {
                "id": "vision-runtime-env",
                "category": "plaintext_credentials_session_tokens_secret_files",
                "path": "local-product/smolvlm-runtime/vision.env",
                "treatment": "metadata_count_only",
            },
            {
                "id": "task-postgres-physical-store",
                "category": "postgres_physical_database_files",
                "path": "postgres-data",
                "treatment": "logical_dump_only",
            },
            {
                "id": "acceptance-evidence",
                "category": "logs_evidence_test_fixtures",
                "path": "evidence",
                "treatment": "metadata_count_only",
            },
        ],
    }


def _quiescence(state_root: Path) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    return {
        "format": BACKUP.QUIESCENCE_FORMAT,
        "status": "QUIESCENT",
        "state_root": str(state_root),
        "compose_project": BACKUP.PROJECT_NAME,
        "quiescent_components": sorted(BACKUP.QUIESCENT_COMPONENTS),
        "effects_suspended": True,
        "outbound_sends_suspended": True,
        "schedules_suspended": True,
        "automation_replay_disabled": True,
        "owner": "fixture-test",
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + dt.timedelta(minutes=45)).isoformat().replace("+00:00", "Z"),
    }


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot_id: str = SNAPSHOT_ID,
) -> tuple[Path, Path, dict[str, Any], Path, Path]:
    state_root = tmp_path / "cyrene-aether-wsl-acceptance"
    state_root.mkdir(mode=0o700)
    state_root.chmod(0o700)
    monkeypatch.setattr(BACKUP, "STATE_DIR", state_root)
    monkeypatch.setattr(BACKUP, "DEFAULT_OUTPUT_DIR", tmp_path / "backups")
    monkeypatch.setattr(BACKUP, "_verify_local_docker_engine", lambda: None)
    monkeypatch.setattr(BACKUP, "_verify_postgres_container", lambda: "fixture-container-id")
    monkeypatch.setattr(
        BACKUP,
        "_postgres_physical_inventory",
        lambda _container_id: {"files": 7, "bytes": 4096},
    )
    monkeypatch.setattr(BACKUP, "_pg_database_inventory", lambda: 0)
    monkeypatch.setattr(
        BACKUP,
        "_pg_table_inventory",
        lambda: ({"public.tasks": 2}, {"public.api_tokens": 1}),
    )
    (state_root / "postgres-data").mkdir(mode=0o700)
    (state_root / "evidence").mkdir(mode=0o700)
    (state_root / "evidence/fixture.json").write_text("{}\n", encoding="utf-8")
    (state_root / "postgres.env").write_text("DATABASE_PASSWORD=not-read\n", encoding="utf-8")
    local_product = state_root / "local-product"
    local_product.mkdir(mode=0o700)
    (local_product / "services.env").write_text("BEARER_TOKEN=not-read\n", encoding="utf-8")
    (local_product / "smolvlm-runtime").mkdir(mode=0o700)
    (local_product / "smolvlm-runtime/vision.env").write_text(
        "VISION_TOKEN=not-read\n", encoding="utf-8"
    )
    _database(
        state_root / "navigator/navigator.sqlite3",
        ("profiles", "personas", "policies", "tasks", "schedules", "audit_events", "plugin_state"),
    )
    _database(
        state_root / "navigator/navigator.sqlite3.conversation-memory.sqlite3",
        ("conversations", "messages", "memories", "memory_sources"),
    )
    _database(
        state_root / "navigator/navigator.sqlite3.conversation-domains.sqlite3",
        (
            "graph_nodes",
            "graph_edges",
            "notes",
            "skills",
            "watches",
            "focus_items",
            "handovers",
            "reviews",
        ),
    )
    dsh_home = state_root / "navigator/dsh-home"
    dsh_home.mkdir(mode=0o700, parents=True)
    (dsh_home / "schedule-state.json").write_text('{"pending":1}\n', encoding="utf-8")
    (dsh_home / "schedule-state.json").chmod(0o600)
    (dsh_home / "run.log").write_text("fixture log\n", encoding="utf-8")
    (dsh_home / "run.log").chmod(0o600)
    exchange_database = state_root / "local-product/exchange-product.sqlite3"
    _database(exchange_database, ("exchanges", "route_audit"))
    _exchange_credentials(exchange_database)
    _database(
        state_root / "navigator/providers/memory.sqlite3",
        ("memories", "memory_chunks"),
        secret=True,
    )
    plugin_db = sqlite3.connect(state_root / "navigator/providers/memory.sqlite3")
    try:
        plugin_db.execute(
            "CREATE TABLE connector_credentials (credential_ref TEXT NOT NULL, workspace_id TEXT NOT NULL)"
        )
        plugin_db.execute(
            "INSERT INTO connector_credentials VALUES (?, ?)",
            ("cred:cyrene/aether-wsl-product-data", "local"),
        )
        plugin_db.commit()
    finally:
        plugin_db.close()
    _database(
        state_root / "navigator/providers/knowledge-graph.sqlite3",
        ("graph_nodes", "graph_edges"),
    )
    attachment_root = state_root / "navigator/navigator.sqlite3.work-attachments"
    attachment_root.mkdir(mode=0o700, parents=True)
    payload = (b"attachment-payload-" * 131_072) + b"tail"
    (attachment_root / "media.bin").write_bytes(payload)
    (attachment_root / "media.bin").chmod(0o600)
    (attachment_root / "public-web-secrets.env").write_text(
        "SECRET_SENTINEL=never-export-this-secret\n", encoding="utf-8"
    )
    (attachment_root / "public-web-secrets.env").chmod(0o600)
    artifact_root = state_root / "navigator/artifacts/blobs/sha256/ab"
    artifact_root.mkdir(mode=0o700, parents=True)
    (artifact_root / "abcdef.bin").write_bytes(b"published-artifact" * 4096)
    (artifact_root / "abcdef.bin").chmod(0o600)
    plugin_blob_root = state_root / "plugins/state/blobs"
    plugin_blob_root.mkdir(mode=0o700, parents=True)
    (plugin_blob_root / "blob-0001.bin").write_bytes(b"plugin-blob" * 4096)
    (plugin_blob_root / "blob-0001.bin").chmod(0o600)
    ref_path = state_root / "models/model-reference.json"
    ref_path.parent.mkdir(mode=0o700, parents=True)
    ref_path.write_text('{"source":"fixture","sha256":"' + "a" * 64 + '"}\n', encoding="utf-8")
    ref_path.chmod(0o600)
    catalog = _catalog(state_root)
    catalog_path = state_root / "source-catalog.json"
    _private_json(catalog_path, catalog)
    quiescence_path = state_root / "quiescence.json"
    _private_json(quiescence_path, _quiescence(state_root))

    def fake_pg_dump(destination: Path) -> dict[str, Any]:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(PG_FIXTURE)
        destination.chmod(0o600)
        return {
            "sha256": hashlib.sha256(PG_FIXTURE).hexdigest(),
            "size_bytes": len(PG_FIXTURE),
            "table_counts": {"public.tasks": 2, "public.api_tokens": 0},
            "excluded_secret_tables": {"public.api_tokens": 1},
            "physical_database_files": {"files": 7, "bytes": 4096},
            "restore_drill_databases": 0,
            "format": "pg_dump_custom",
        }

    monkeypatch.setattr(BACKUP, "_pg_dump", fake_pg_dump)
    monkeypatch.setattr(BACKUP, "_verify_pg_archive", lambda _path: None)
    output_dir = tmp_path / "backups"
    return state_root, output_dir, catalog, catalog_path, quiescence_path


def test_full_state_backup_restore_and_safe_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconciles operational table categories, attachments, secrets, and restore state."""
    state_root, output_dir, _catalog_value, catalog_path, quiescence_path = _fixture(
        tmp_path, monkeypatch
    )
    original = sqlite3.connect(
        state_root / "navigator/navigator.sqlite3.conversation-memory.sqlite3"
    )
    try:
        original.execute("INSERT INTO conversations (value) VALUES ('wal-visible-row')")
        original.commit()
    finally:
        original.close()

    backup_args = SimpleNamespace(
        catalog=str(catalog_path),
        quiescence_receipt=str(quiescence_path),
        output_dir=str(output_dir),
        snapshot_id=SNAPSHOT_ID,
    )
    assert BACKUP.cmd_backup(backup_args) == 0
    snapshot = output_dir / SNAPSHOT_ID
    assert BACKUP.cmd_verify(SimpleNamespace(snapshot=str(snapshot))) == 0

    archive, manifest, receipt = BACKUP._read_snapshot(snapshot)
    assert receipt["archive_sha256"]
    assert set(manifest["required_categories"]) == BACKUP.REQUIRED_CATEGORIES
    assert (
        manifest["source_version_sha256"]
        == hashlib.sha256(BACKUP._json_bytes(manifest["source_revisions"])).hexdigest()
    )
    for resource in manifest["resources"]:
        assert (
            resource["source_revision_sha256"]
            == hashlib.sha256(resource["source_revision"].encode()).hexdigest()
        )
    navigator_resource = next(
        item for item in manifest["resources"] if item["id"] == "navigator-business"
    )
    assert navigator_resource["table_counts"]["personas"] == 1
    assert navigator_resource["table_counts"]["policies"] == 1
    conversation_resource = next(
        item for item in manifest["resources"] if item["id"] == "navigator-conversation-memory"
    )
    assert conversation_resource["table_counts"]["conversations"] == 2
    domain_resource = next(
        item for item in manifest["resources"] if item["id"] == "navigator-conversation-domains"
    )
    assert domain_resource["table_counts"]["reviews"] == 1
    exchange_resource = next(
        item for item in manifest["resources"] if item["id"] == "exchange-state-and-audit"
    )
    assert exchange_resource["table_counts"]["api_credentials"] == 3
    assert exchange_resource["approved_verifier_fields"] == {
        "api_credentials.token_digest": {"format": "sha256_lowercase_hex", "validated_rows": 3}
    }
    assert "api_credentials" not in exchange_resource["excluded_secret_tables"]
    plugin_resource = next(
        item for item in manifest["resources"] if item["id"] == "plugin-database"
    )
    assert plugin_resource["table_counts"]["connector_credentials"] == 1
    assert plugin_resource["excluded_secret_tables"]["plugin_credentials"] == 0
    assert (
        next(item for item in manifest["resources"] if item["id"] == "task-postgres")[
            "table_counts"
        ]["public.tasks"]
        == 2
    )
    assert (
        manifest["excluded_categories"]["plaintext_credentials_session_tokens_secret_files"][
            "files"
        ]
        == 4
    )
    assert (
        manifest["excluded_categories"]["plaintext_credentials_session_tokens_secret_files"][
            "database_rows"
        ]
        == 1
    )
    assert set(manifest["external_secret_refs"]) == {
        "env:CYRENE_EXCHANGE_BEARER_TOKEN",
        "cred:cyrene/aether-wsl-product-data",
    }
    assert manifest["excluded_categories"]["models_toolchain_and_cache"]["copied"] == 0
    assert manifest["excluded_categories"]["logs_evidence_test_fixtures"]["files"] == 2
    assert manifest["excluded_categories"]["postgres_physical_database_files"]["files"] == 7
    assert manifest["excluded_categories"]["postgres_physical_database_files"]["copied"] == 0
    assert manifest["excluded_categories"]["postgres_restore_drill_databases"]["databases"] == 0
    with tarfile.open(archive, mode="r:gz") as tar:
        pg_dump_member = tar.extractfile("payload/task-postgres/database.custom")
        assert pg_dump_member is not None and pg_dump_member.read() == PG_FIXTURE

    target = tmp_path / "cyrene-aether-wsl-restore-abcdef123456"
    pg_restore_calls: list[tuple[str, dict[str, int]]] = []

    def fake_pg_restore(path: Path, database: str, counts: dict[str, int]) -> None:
        assert path.read_bytes() == PG_FIXTURE
        pg_restore_calls.append((database, counts))

    monkeypatch.setattr(BACKUP, "_pg_restore", fake_pg_restore)
    assert (
        BACKUP.cmd_restore(
            SimpleNamespace(
                snapshot=str(snapshot),
                quiescence_receipt=str(quiescence_path),
                target_root=str(target),
            )
        )
        == 0
    )
    assert pg_restore_calls and pg_restore_calls[0][0].startswith("aether_restore_")
    restored = sqlite3.connect(target / "navigator/navigator.sqlite3")
    try:
        assert restored.execute("SELECT COUNT(*) FROM personas").fetchone() == (1,)
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        restored.close()
    restored_memory = sqlite3.connect(
        target / "navigator/navigator.sqlite3.conversation-memory.sqlite3"
    )
    try:
        assert restored_memory.execute("SELECT COUNT(*) FROM conversations").fetchone() == (2,)
    finally:
        restored_memory.close()
    restored_plugin = sqlite3.connect(target / "navigator/providers/memory.sqlite3")
    try:
        assert restored_plugin.execute("SELECT COUNT(*) FROM plugin_credentials").fetchone() == (0,)
        assert restored_plugin.execute(
            "SELECT credential_ref FROM connector_credentials"
        ).fetchone() == ("cred:cyrene/aether-wsl-product-data",)
    finally:
        restored_plugin.close()
    restored_exchange = sqlite3.connect(target / "local-product/exchange-product.sqlite3")
    try:
        assert restored_exchange.execute("SELECT COUNT(*) FROM api_credentials").fetchone() == (3,)
        assert restored_exchange.execute(
            "SELECT COUNT(*) FROM api_credentials WHERE length(token_digest) = 64 "
            "AND token_digest NOT GLOB '*[^0-9a-f]*'"
        ).fetchone() == (3,)
    finally:
        restored_exchange.close()
    assert (target / "navigator/navigator.sqlite3.work-attachments/media.bin").read_bytes() == (
        state_root / "navigator/navigator.sqlite3.work-attachments/media.bin"
    ).read_bytes()
    assert not (
        target / "navigator/navigator.sqlite3.work-attachments/public-web-secrets.env"
    ).exists()
    assert (target / "navigator/dsh-home/schedule-state.json").is_file()
    assert not (target / "navigator/dsh-home/run.log").exists()
    assert (target / "navigator/artifacts/blobs/sha256/ab/abcdef.bin").read_bytes() == (
        state_root / "navigator/artifacts/blobs/sha256/ab/abcdef.bin"
    ).read_bytes()
    restore_receipt = target / "restore-receipt.json"
    restore_value = json.loads(restore_receipt.read_text(encoding="utf-8"))
    assert restore_value["status"] == "RESTORED"
    assert restore_value["schedules_suspended"] is True
    assert BACKUP.cmd_rollback(SimpleNamespace(restore_receipt=str(restore_receipt))) == 0
    rollback = json.loads((target / "ROLLBACK.json").read_text(encoding="utf-8"))
    assert rollback["status"] == "ROLLED_BACK_TO_PRESERVED_ORIGINAL"
    assert rollback["original_state_modified"] is False
    assert target.exists()
    original = sqlite3.connect(
        state_root / "navigator/navigator.sqlite3.conversation-memory.sqlite3"
    )
    try:
        assert original.execute("SELECT COUNT(*) FROM conversations").fetchone() == (2,)
    finally:
        original.close()


def test_missing_category_or_stale_quiescence_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, output_dir, catalog, catalog_path, quiescence_path = _fixture(tmp_path, monkeypatch)
    catalog["sources"] = [item for item in catalog["sources"] if item["id"] != "plugin-blobs"]
    _private_json(catalog_path, catalog)
    with pytest.raises(BACKUP.BackupError, match="incomplete"):
        BACKUP.cmd_backup(
            SimpleNamespace(
                catalog=str(catalog_path),
                quiescence_receipt=str(quiescence_path),
                output_dir=str(output_dir),
                snapshot_id=SNAPSHOT_ID,
            )
        )
    catalog = _catalog(state_root)
    _private_json(catalog_path, catalog)
    quiescence = _quiescence(state_root)
    quiescence["schedules_suspended"] = False
    _private_json(quiescence_path, quiescence)
    with pytest.raises(BACKUP.BackupError, match="suspend effects"):
        BACKUP.cmd_backup(
            SimpleNamespace(
                catalog=str(catalog_path),
                quiescence_receipt=str(quiescence_path),
                output_dir=str(output_dir),
                snapshot_id=SNAPSHOT_ID,
            )
        )


def test_sqlite_credential_rows_fail_before_secret_data_reaches_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state_root, output_dir, _catalog, catalog_path, quiescence_path = _fixture(
        tmp_path, monkeypatch
    )
    sensitive_db = sqlite3.connect(Path(BACKUP.STATE_DIR) / "navigator/providers/memory.sqlite3")
    try:
        sensitive_db.execute(
            "INSERT INTO plugin_credentials (access_token) VALUES (?)",
            ("must-never-reach-staging",),
        )
        sensitive_db.commit()
    finally:
        sensitive_db.close()

    with pytest.raises(BACKUP.BackupError, match="candidate credential rows"):
        BACKUP.cmd_backup(
            SimpleNamespace(
                catalog=str(catalog_path),
                quiescence_receipt=str(quiescence_path),
                output_dir=str(output_dir),
                snapshot_id=SNAPSHOT_ID,
            )
        )
    assert not output_dir.exists()


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("unknown_column", "schema differs from the approved schema"),
        ("invalid_digest", "verifier format is invalid"),
    ],
)
def test_exchange_verifier_exception_requires_exact_schema_and_digest_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_error: str,
) -> None:
    _state_root, output_dir, _catalog, catalog_path, quiescence_path = _fixture(
        tmp_path, monkeypatch
    )
    exchange_database = sqlite3.connect(
        Path(BACKUP.STATE_DIR) / "local-product/exchange-product.sqlite3"
    )
    try:
        if mutation == "unknown_column":
            exchange_database.execute("ALTER TABLE api_credentials ADD COLUMN bearer_token TEXT")
        else:
            exchange_database.execute(
                "UPDATE api_credentials SET token_digest = 'invalid-verifier' WHERE rowid = 1"
            )
        exchange_database.commit()
    finally:
        exchange_database.close()

    with pytest.raises(BACKUP.BackupError, match=expected_error):
        BACKUP.cmd_backup(
            SimpleNamespace(
                catalog=str(catalog_path),
                quiescence_receipt=str(quiescence_path),
                output_dir=str(output_dir),
                snapshot_id=SNAPSHOT_ID,
            )
        )
    assert not output_dir.exists()


def test_token_digest_in_non_exchange_database_remains_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state_root, output_dir, _catalog, catalog_path, quiescence_path = _fixture(
        tmp_path, monkeypatch
    )
    plugin_database = sqlite3.connect(
        Path(BACKUP.STATE_DIR) / "navigator/providers/knowledge-graph.sqlite3"
    )
    try:
        plugin_database.execute("CREATE TABLE api_credentials (token_digest TEXT NOT NULL)")
        plugin_database.execute("INSERT INTO api_credentials VALUES (?)", ("a" * 64,))
        plugin_database.commit()
    finally:
        plugin_database.close()

    with pytest.raises(BACKUP.BackupError, match="candidate credential rows"):
        BACKUP.cmd_backup(
            SimpleNamespace(
                catalog=str(catalog_path),
                quiescence_receipt=str(quiescence_path),
                output_dir=str(output_dir),
                snapshot_id=SNAPSHOT_ID,
            )
        )
    assert not output_dir.exists()


def test_tree_copy_rejects_a_source_that_changes_during_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"stable-payload")
    destination = tmp_path / "private" / "copy.bin"
    original_copy = BACKUP._copy_stream

    def mutate_after_copy(input_stream: Any, output_stream: Any) -> tuple[str, int]:
        result = original_copy(input_stream, output_stream)
        source.write_bytes(b"changed-payload")
        return result

    monkeypatch.setattr(BACKUP, "_copy_stream", mutate_after_copy)
    with pytest.raises(BACKUP.BackupError, match="source changed while copying"):
        BACKUP._copy_regular(source, destination)


def test_postgres_inventory_rejects_unlisted_databases_and_counts_restore_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    databases = [
        "cyrene_aether",
        "postgres",
        "aether_restore_abcdef1234_abcdef",
    ]

    def query(statement: str, *, database: str = "cyrene_aether") -> list[str]:
        if "FROM pg_database" in statement:
            return databases
        assert database == "postgres"
        return ["0"]

    monkeypatch.setattr(BACKUP, "_pg_query", query)
    assert BACKUP._pg_database_inventory() == 1
    databases.append("unregistered_plugin_store")
    with pytest.raises(BACKUP.BackupError, match="unlisted application database"):
        BACKUP._pg_database_inventory()


@pytest.mark.parametrize("damage", ["truncated", "corrupted"])
def test_corrupt_archive_is_rejected_even_with_recomputed_outer_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    _state_root, output_dir, _catalog, catalog_path, quiescence_path = _fixture(
        tmp_path, monkeypatch
    )
    BACKUP.cmd_backup(
        SimpleNamespace(
            catalog=str(catalog_path),
            quiescence_receipt=str(quiescence_path),
            output_dir=str(output_dir),
            snapshot_id=SNAPSHOT_ID,
        )
    )
    snapshot = output_dir / SNAPSHOT_ID
    archive_path = snapshot / "snapshot.tar.gz"
    archive_bytes = bytearray(archive_path.read_bytes())
    if damage == "truncated":
        archive_bytes = archive_bytes[:-24]
    else:
        archive_bytes[len(archive_bytes) // 2] ^= 0x01
    archive_path.write_bytes(archive_bytes)
    archive_path.chmod(0o600)
    receipt_path = snapshot / BACKUP.RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["archive_sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    receipt["archive_size_bytes"] = archive_path.stat().st_size
    _private_json(receipt_path, receipt)
    with pytest.raises(BACKUP.BackupError, match="truncated or corrupt"):
        BACKUP.cmd_verify(SimpleNamespace(snapshot=str(snapshot)))


def test_interrupted_snapshot_is_retained_and_new_run_can_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state_root, output_dir, _catalog, catalog_path, quiescence_path = _fixture(
        tmp_path, monkeypatch
    )
    original_stage = BACKUP._stage_resource
    calls = 0

    def interrupted(resource: dict[str, Any], staging: Path) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption")
        return original_stage(resource, staging)

    monkeypatch.setattr(BACKUP, "_stage_resource", interrupted)
    with pytest.raises(OSError, match="simulated interruption"):
        BACKUP.cmd_backup(
            SimpleNamespace(
                catalog=str(catalog_path),
                quiescence_receipt=str(quiescence_path),
                output_dir=str(output_dir),
                snapshot_id=SNAPSHOT_ID,
            )
        )
    partial = output_dir / f".{SNAPSHOT_ID}.partial"
    assert partial.is_dir()
    assert (
        json.loads((partial / "journal.json").read_text(encoding="utf-8"))["state"] == "INTERRUPTED"
    )
    assert not (output_dir / SNAPSHOT_ID).exists()

    monkeypatch.setattr(BACKUP, "_stage_resource", original_stage)
    resumed_id = "20261031T220100Z-abcdef123457"
    assert (
        BACKUP.cmd_backup(
            SimpleNamespace(
                catalog=str(catalog_path),
                quiescence_receipt=str(quiescence_path),
                output_dir=str(output_dir),
                snapshot_id=resumed_id,
            )
        )
        == 0
    )
    assert partial.is_dir()
    assert (output_dir / resumed_id / "snapshot.tar.gz").is_file()


def test_source_symlink_and_restore_overwrite_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, output_dir, catalog, catalog_path, quiescence_path = _fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside.sqlite3"
    _database(outside, ("outside",))
    source = state_root / "navigator/navigator.sqlite3"
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(BACKUP.BackupError, match="symlink"):
        BACKUP._validate_catalog(catalog)

    source.unlink()
    _database(source, BUSINESS_TABLES[:15])
    BACKUP.cmd_backup(
        SimpleNamespace(
            catalog=str(catalog_path),
            quiescence_receipt=str(quiescence_path),
            output_dir=str(output_dir),
            snapshot_id=SNAPSHOT_ID,
        )
    )
    existing_target = tmp_path / "cyrene-aether-wsl-restore-abcdef123456"
    existing_target.mkdir(mode=0o700)
    with pytest.raises(BACKUP.BackupError, match="must not exist"):
        BACKUP.cmd_restore(
            SimpleNamespace(
                snapshot=str(output_dir / SNAPSHOT_ID),
                quiescence_receipt=str(quiescence_path),
                target_root=str(existing_target),
            )
        )
