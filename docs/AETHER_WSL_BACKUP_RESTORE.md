# Aether WSL Application-State Backup and Restore

This guide describes a full operational-state snapshot for the isolated Aether acceptance stack on Ubuntu 24.04 WSL2 x86_64. The fixed state root is `/home/baijin/.local/state/cyrene-aether-wsl-acceptance`; the fixed Compose project is `cyrene-aether-wsl-acceptance`.

The snapshot covers every resource declared by a complete source catalog: Navigator business databases, DSH durable tasks and schedules, attachment files, Exchange persistence and audit data, stateful Plugin databases and blob stores, and the task PostgreSQL database. PostgreSQL is captured with `pg_dump`; copying its live data directory is prohibited. A catalog that omits any required category is rejected. A partial conversation import is not a full application-state backup.

## Before a backup

Run the utility only in the Ubuntu 24.04 WSL2 x86_64 acceptance environment with a local Unix Docker socket. The utility rejects remote Docker contexts. The acceptance owner must first pause the exact task-stack writers, suspend external sends and platform writes, and suspend schedules and automation replay. Do not stop services by port or process ID. The utility does not stop or restart services and does not change the cloud VM, cloud services, Bot, or ingress.

The acceptance owner writes two private JSON files under the task state root:

- `backup-source-catalog.json` enumerates all canonical stores, exact source revisions, the external secret references required to reopen the environment, and explicitly counted exclusion roots.
- `backup-quiescence.json` records the owner, the fixed project and state root, the quiescent components, suspended effects, and a short expiry window.

Both files must be owned by the current user and have mode `0600`. The source catalog must use format `cyrene.aether.backup-catalog.v1`, contain the exact fixed `state_root` and `compose_project`, and provide 40-character commit IDs for `navigator`, `exchange`, `plugins`, and `workspace`.

Each source record has a unique `id`, an `owner`, a `kind`, one or more `categories`, and `secret_policy: "external_refs_only"`. SQLite and file-tree records also require a state-root-relative `path`. A PostgreSQL record must identify service `postgres` and database `cyrene_aether`; it must not name a filesystem path. Large model, toolchain, and cache resources use `kind: "reference"` with a `model_reference`, `toolchain_reference`, or `cache_reference` category. The utility hashes these resources and records their file and byte counts without copying them.

The required operational categories are:

| Category | Required content |
| --- | --- |
| `navigator_business` | All canonical Navigator business SQLite databases, including memory, profile, graph, policy, persona, note, skill, watch, focus, handover, review, and conversation state where those stores exist. |
| `navigator_dsh` | DSH durable task, handover, and schedule state. |
| `navigator_attachments` | Authorized attachment and media artifacts. Generated test fixtures are excluded and counted. |
| `exchange_database` | Exchange persisted application database. |
| `exchange_audit` | Exchange audit history, in the authoritative audit store. |
| `plugin_database` | Every stateful Plugin-owned database. |
| `plugin_blobs` | Every stateful Plugin-owned blob store. |
| `postgres_database` | The complete task database `cyrene_aether`, exported through `pg_dump`. |

If Exchange audit is stored in the same SQLite database as Exchange application state, one resource may carry both `exchange_database` and `exchange_audit`. A database or directory can satisfy multiple categories only when it is the canonical store for each one. Add every distinct store as its own resource.

`declared_exclusions` must include these categories and metadata-only treatments:

| Category | Treatment | Recorded counts |
| --- | --- | --- |
| `plaintext_credentials_session_tokens_secret_files` | `metadata_count_only` | Excluded file count and bytes. The tool never reads the file contents. |
| `postgres_physical_database_files` | `logical_dump_only` | Physical file count and bytes; copied count remains zero because the database is represented by `pg_dump`. |
| `logs_evidence_test_fixtures` | `metadata_count_only` | Excluded file count and bytes. |

The catalog should list the known environment files individually, such as `postgres.env`, `local-product/services.env`, and runtime `*.env` files. Tree sources also omit credential-looking names, logs, evidence, fixtures, and cache paths by name. The catalog must list external secret references as opaque identifiers, for example `env:CYRENE_EXCHANGE_BEARER_TOKEN` or `cred:cyrene/aether-wsl-product-data`; never put secret values in either JSON file.

SQLite uses the online backup API and reconciles every included table count. Before writing any SQLite snapshot to disk, the tool examines table and column names for credential-bearing schemas. If a candidate credential table contains rows, backup fails before creating a database payload; move those values to external secret storage and retain only references before retrying. PostgreSQL schema-detected secret tables keep their schema but their table data is excluded from `pg_dump`. Every source owner must attest that credentials are represented only by external references. This schema check does not inspect arbitrary user-authored text or binary attachment contents.

The quiescence receipt is an owner attestation rather than a process or port probe. It must name all of `navigator`, `dsh`, `exchange`, `plugins`, and `postgres`, and set `effects_suspended`, `outbound_sends_suspended`, `schedules_suspended`, and `automation_replay_disabled` to `true`. Its expiry may be no more than two hours after creation. The utility rejects an expired, incomplete, or incorrectly scoped receipt.

## Create and verify a snapshot

```bash
umask 077
python3 scripts/aether_wsl_backup.py backup \
  --catalog /home/baijin/.local/state/cyrene-aether-wsl-acceptance/backup-source-catalog.json \
  --quiescence-receipt /home/baijin/.local/state/cyrene-aether-wsl-acceptance/backup-quiescence.json

python3 scripts/aether_wsl_backup.py verify \
  --snapshot /home/baijin/.local/state/cyrene-aether-wsl-backups/<snapshot-id>
```

The default snapshot directory is `~/.local/state/cyrene-aether-wsl-backups`, outside the live state root. The output is private and contains a compressed archive, a manifest, an outer hash receipt, and a completion journal. The manifest records source and source-version hashes, file hashes and sizes, SQLite/PostgreSQL table counts, exclusion counts, external secret references, and the quiescence receipt digest. `verify` rejects a truncated or corrupt archive, duplicate or unsafe member paths, missing files, digest mismatches, invalid SQLite databases, and count mismatches.

An interrupted run leaves a hidden `.partial` directory and journal. It is never treated as a completed snapshot or resumed in place. Retain it for inspection and start a later run with a new snapshot ID after the source is quiescent again. Partial payload files and secret source files are never published as a valid snapshot.

## Restore into a fresh namespace

Coordinate another quiescence window with the acceptance owner before using the task PostgreSQL container. Restore requires a fresh `backup-quiescence.json` receipt for that window. The restore target must be a new sibling directory named `cyrene-aether-wsl-restore-<id>`; the utility refuses an existing target and does not support path remapping. PostgreSQL restore creates a separate generated database name inside the fixed task project. It never overwrites `cyrene_aether` or another database.

```bash
python3 scripts/aether_wsl_backup.py restore \
  --snapshot /home/baijin/.local/state/cyrene-aether-wsl-backups/<snapshot-id> \
  --quiescence-receipt /home/baijin/.local/state/cyrene-aether-wsl-acceptance/backup-quiescence.json \
  --target-root /home/baijin/.local/state/cyrene-aether-wsl-restore-<id>
```

Restore validates the snapshot before creating its target, extracts only regular files below the new private root, checks every hash, and reconciles restored table/file counts. It writes `RESTORE_SUSPENDED.json` with effects, outbound sends, schedules, and replay disabled. It does not start application workers, replay tasks, contact providers, or perform a historical-data cutover. Keep the source stack and its original databases unchanged throughout this drill.

If restore fails or is interrupted, the target directory, partial database namespace, and failure receipt remain for inspection. Do not reuse or delete them as part of a retry; choose a new target and database namespace after reviewing the failure.

## Roll back the restore drill

The original state remains authoritative because restore writes only to a fresh target. `rollback` records that the new target is inactive, keeps it for audit, and records the original database as the preserved source. It does not delete files or databases and does not activate another state root.

```bash
python3 scripts/aether_wsl_backup.py rollback \
  --restore-receipt /home/baijin/.local/state/cyrene-aether-wsl-restore-<id>/restore-receipt.json
```

The rollback receipt states that the original state was not modified, the restored target was retained, and no cutover occurred. A future production cutover would require a separate change and acceptance process.

All path checks reject symlinks, foreign-owned application files, traversal, and archive links. Snapshot publication is atomic. No cloud, VM, Bot, ingress, model, PostgreSQL volume, or search service is pruned or recreated by the backup operation.

The focused unit suite uses real temporary SQLite databases and attachments, but its PostgreSQL adapter is a simulated fixture. It proves the archive, path, and count logic; it is not a real PostgreSQL roundtrip. Record a real `pg_dump`/fresh-database `pg_restore` readback separately after the acceptance owner provides an exact task-stack quiescence window and the complete store catalog.

---
<!-- Chinese Translation / 中文翻译 -->

# Aether WSL 应用状态备份与恢复

本文说明如何在 Ubuntu 24.04 WSL2 x86_64 的隔离 Aether 验收栈中创建完整的运行状态快照。固定状态根目录为 `/home/baijin/.local/state/cyrene-aether-wsl-acceptance`，固定 Compose project 为 `cyrene-aether-wsl-acceptance`。

快照覆盖完整 source catalog 中声明的每项资源：Navigator 业务数据库、DSH 持久任务和日程、附件文件、Exchange 持久化与审计数据、有状态 Plugin 数据库和 blob store，以及 task PostgreSQL 数据库。PostgreSQL 必须通过 `pg_dump` 逻辑导出，禁止复制活动数据目录。catalog 缺少任一必需类别时，工具会拒绝备份。conversation migration 的部分导入不算完整应用状态备份。

## 备份前

仅在 Ubuntu 24.04 WSL2 x86_64 验收环境及本机 Unix Docker socket 下运行该工具。工具会拒绝远端 Docker context。验收 owner 必须先暂停 task stack 中对应的所有 writer，暂停外发和 platform 写入，并暂停日程与自动重放。不得按端口或 PID 停止服务。工具不会停止或重启服务，也不会更改云端 VM、云服务、Bot 或 ingress。

验收 owner 在 task 状态根目录下写入两个私有 JSON 文件：

- `backup-source-catalog.json` 枚举所有规范 store、精确源码版本、重新启用环境所需的外部 secret reference，以及显式计数的排除目录。
- `backup-quiescence.json` 记录 owner、固定 project 和状态根目录、已静止的组件、暂停的副作用，以及短时有效期。

两个文件都必须归当前用户所有且权限为 `0600`。source catalog 使用格式 `cyrene.aether.backup-catalog.v1`，填写精确的固定 `state_root` 与 `compose_project`，并为 `navigator`、`exchange`、`plugins` 和 `workspace` 提供 40 位 commit ID。

每项 source 记录都有唯一 `id`、`owner`、`kind`、一个或多个 `categories`，以及 `secret_policy: "external_refs_only"`。SQLite 和文件树还需要相对于状态根目录的 `path`。PostgreSQL 记录必须指向 `postgres` service 和 `cyrene_aether` 数据库；不得填写文件系统路径。大型模型、toolchain 和 cache 使用 `kind: "reference"`，并选择 `model_reference`、`toolchain_reference` 或 `cache_reference` 类别。工具只哈希这些资源并记录文件数和字节数，不复制内容。

必需的运行状态类别如下：

| 类别 | 必需内容 |
| --- | --- |
| `navigator_business` | 所有规范 Navigator 业务 SQLite 数据库；按实际 store 覆盖 memory、profile、graph、policy、persona、note、skill、watch、focus、handover、review 与 conversation 状态。 |
| `navigator_dsh` | DSH 持久任务、handover 与 schedule 状态。 |
| `navigator_attachments` | 已授权的附件与媒体文件；生成的测试 fixture 要排除并计数。 |
| `exchange_database` | Exchange 持久化应用数据库。 |
| `exchange_audit` | Exchange 规范审计存储中的历史记录。 |
| `plugin_database` | 每个有状态 Plugin 所有的数据库。 |
| `plugin_blobs` | 每个有状态 Plugin 所有的 blob store。 |
| `postgres_database` | 通过 `pg_dump` 导出的完整 task 数据库 `cyrene_aether`。 |

如果 Exchange 审计与应用状态保存在同一个 SQLite 数据库中，一项 source 可以同时标记 `exchange_database` 与 `exchange_audit`。只有在同一资源确实是这两类数据的规范存储时才允许多重分类。不同 store 必须分别登记。

`declared_exclusions` 必须包含下列类别，并采用元数据计数方式：

| 类别 | 处理方式 | 记录的计数 |
| --- | --- | --- |
| `plaintext_credentials_session_tokens_secret_files` | `metadata_count_only` | 排除文件数和字节数。工具绝不读取文件内容。 |
| `postgres_physical_database_files` | `logical_dump_only` | PostgreSQL 物理文件数和字节数；复制数必须为零，数据库内容由 `pg_dump` 替代。 |
| `logs_evidence_test_fixtures` | `metadata_count_only` | 排除文件数和字节数。 |

catalog 应逐一列出已知环境文件，例如 `postgres.env`、`local-product/services.env` 和运行时 `*.env` 文件。文件树会按名称排除凭据文件、日志、evidence、fixture 和 cache。外部 secret reference 只能填写不透明标识，例如 `env:CYRENE_EXCHANGE_BEARER_TOKEN` 或 `cred:cyrene/aether-wsl-product-data`；不得在任一 JSON 文件中写入 secret 值。

SQLite 使用 online backup API，并对账每一张被包含的表。写入任何 SQLite 快照前，工具先检查表名和列名中的凭据特征。如果候选凭据表含有记录，工具会在创建数据库 payload 前失败；应先将这些值移入外部 secret storage，并只保留 reference 后再重试。PostgreSQL 中 schema 标记为敏感的表会保留 schema，但其表数据会从 `pg_dump` 中排除。每个 source owner 必须声明 credential 只以 external reference 形式保存。该 schema 检查不会扫描任意用户文本或二进制附件内容。

Quiescence receipt 是 owner attestation，不是进程或端口探测结果。它必须列出 `navigator`、`dsh`、`exchange`、`plugins` 和 `postgres`，并将 `effects_suspended`、`outbound_sends_suspended`、`schedules_suspended` 与 `automation_replay_disabled` 设为 `true`。有效期不得超过创建时间后的两小时。receipt 范围错误、过期或字段不全时，工具会拒绝备份。

## 创建并验证快照

```bash
umask 077
python3 scripts/aether_wsl_backup.py backup \
  --catalog /home/baijin/.local/state/cyrene-aether-wsl-acceptance/backup-source-catalog.json \
  --quiescence-receipt /home/baijin/.local/state/cyrene-aether-wsl-acceptance/backup-quiescence.json

python3 scripts/aether_wsl_backup.py verify \
  --snapshot /home/baijin/.local/state/cyrene-aether-wsl-backups/<snapshot-id>
```

默认快照目录为 live state root 以外的 `~/.local/state/cyrene-aether-wsl-backups`。输出目录为私有权限，包含压缩归档、manifest、外层 hash receipt 和完成 journal。manifest 记录 source 与 source-version hash、文件 hash 和大小、SQLite/PostgreSQL 表计数、排除计数、外部 secret reference，以及 quiescence receipt 摘要。`verify` 会拒绝截断或损坏的归档、重复或不安全路径、缺失文件、摘要不匹配、无效 SQLite 数据库和计数不匹配。

被中断的任务会留下隐藏的 `.partial` 目录和 journal。它不会被当作完成快照，也不能原地续跑。保留它以便检查，并在 source 再次处于 quiescent 状态后使用新的 snapshot ID 启动下一次备份。临时 payload 和 secret source 文件不会被发布成有效快照。

## 恢复到新的命名空间

使用 task PostgreSQL container 前，先与验收 owner 协调另一个 quiescence 窗口。恢复必须提供该窗口新生成的 `backup-quiescence.json` receipt。恢复目标必须是同级的全新目录，名称为 `cyrene-aether-wsl-restore-<id>`；工具会拒绝已存在的目标，也不支持路径映射。PostgreSQL 恢复会在固定 task project 中创建单独的生成数据库名，绝不覆盖 `cyrene_aether` 或其他数据库。

```bash
python3 scripts/aether_wsl_backup.py restore \
  --snapshot /home/baijin/.local/state/cyrene-aether-wsl-backups/<snapshot-id> \
  --quiescence-receipt /home/baijin/.local/state/cyrene-aether-wsl-acceptance/backup-quiescence.json \
  --target-root /home/baijin/.local/state/cyrene-aether-wsl-restore-<id>
```

恢复会先验证快照，再创建目标；只会将普通文件解包到新的私有根目录下，并核对每个摘要及恢复后的表/文件计数。工具会写入 `RESTORE_SUSPENDED.json`，明确关闭副作用、外发、日程和自动重放。它不会启动应用 worker、重放任务、联系 Provider 或执行历史数据切换。演练期间须保持原始 source stack 和数据库不变。

恢复失败或中断时，目标目录、部分数据库命名空间和失败 receipt 都会保留以便检查。检查失败原因后，应选择新的目标和数据库命名空间重试，不要重用或删除旧数据。

## 回滚恢复演练

由于恢复只写入全新目标，原始状态始终保持权威。`rollback` 会记录新目标已停用、将其保留供审计，并将原数据库记为受保护 source。它不会删除文件或数据库，也不会启用另一个状态根目录。

```bash
python3 scripts/aether_wsl_backup.py rollback \
  --restore-receipt /home/baijin/.local/state/cyrene-aether-wsl-restore-<id>/restore-receipt.json
```

rollback receipt 会记录原始状态未被修改、恢复目标仍被保留、没有发生切换。未来正式切换历史数据需要单独的变更和验收流程。

所有路径检查都会拒绝符号链接、归属错误的应用文件、路径遍历和归档链接。快照发布是原子的。备份操作不会清理或重建云端、VM、Bot、ingress、模型、PostgreSQL volume 或搜索服务。

针对性单元测试使用真实临时 SQLite 数据库和附件文件；其中 PostgreSQL adapter 使用 simulated fixture。测试验证了归档、路径和计数逻辑，但不代表真实 PostgreSQL roundtrip。真实 `pg_dump`、新数据库 `pg_restore` 和回读须在验收 owner 提供准确 task-stack quiescence 窗口及完整 store catalog 后另行记录。
