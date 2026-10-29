# Persistence forward recovery / Persistence 向前恢复

This operator-only path handles one narrowly pinned failure: a `navigator-persistence`
image has replaced the service, its exact Navigator source migration changed the SQLite
schema, and the sealed stage journal is `RECOVERY_REQUIRED` with
`database_migration_contract_failed`. It does not edit the sealed plan or original
journal history, restore backup bytes, roll back the Persistence image, or replay the
original apply operation.

该 owner 专用入口只处理一个固定故障：`navigator-persistence` 镜像已替换服务，精确
Navigator 源码迁移改变了 SQLite schema，且 sealed stage journal 的状态为
`RECOVERY_REQUIRED`、错误码为 `database_migration_contract_failed`。它不会编辑 sealed
plan 或原 journal 历史、恢复备份字节、回滚 Persistence 镜像，也不会重放原 apply。

Run `prepare` as root while the operation still holds Teams ingress and the original
Navigator drain. It runs the candidate image by immutable image ID in a networkless,
secretless container using an independent copy of the sealed SQLite backup. The image
must expose the pinned `PersistenceStore`, `WorkStore`, and startup API source files with
their expected SHA-256 values. The migration sequence runs twice; both results must be
identical. The resulting full schema and per-table row digests must match the live
database exactly, and all service, writer-lease, ingress, Navigator-drain, selector,
plan, and backup guards must still hold.

确认操作仍持有 Teams ingress 且 Navigator 维持原 drain 后，以 root 运行 `prepare`。该步
以不可变 image ID 启动候选镜像，在无网络、无凭据的容器内对 sealed SQLite 备份的独立副本
执行迁移。镜像内 `PersistenceStore`、`WorkStore` 和启动 API 的源码文件必须与固定 SHA-256
完全一致；迁移运行两次且结果必须相同。迁移后的完整 schema 和每张表的行摘要必须与线上
数据库完全一致，同时确认所有服务、写租约、ingress、Navigator drain、selector、plan 和
备份条件仍满足。

```sh
python3 "$DEPLOYMENT/persistence_forward_recovery.py" prepare \
  --operation-id "$OPERATION_ID" \
  --journal-dir "$JOURNAL_DIR" \
  --preview-file "$PLAN_DIR/persistence-forward-recovery-preview.json"
```

Review the returned preview digest and evidence. Only the exact digest authorizes the
separate `commit` command:

```sh
python3 "$DEPLOYMENT/persistence_forward_recovery.py" commit \
  --operation-id "$OPERATION_ID" \
  --journal-dir "$JOURNAL_DIR" \
  --preview-file "$PLAN_DIR/persistence-forward-recovery-preview.json" \
  --confirm-preview-sha256 "$REVIEWED_PREVIEW_SHA256" \
  --receipt-file "$PLAN_DIR/$OPERATION_ID.persistence-forward-recovery-receipt.json"
```

`commit` re-runs the read-only proof under the canonical operation lock, writes a
separate owner-only authorization receipt, then completes only the existing post-target
tail: resume the original Navigator generation, read resource limits, promote the
sealed selector, resume this operation's ingress hold, and read back the final delivery
revision. The journal retains its migration-gate failure history and records the
forward-recovery receipt and new phases. Deployment reconciliation can then continue
the remaining independently planned release targets.

`commit` 会在 canonical operation lock 内重新运行只读证明，写入单独的 owner-only 授权回执，
随后只完成原有 target 后续步骤：恢复原 Navigator generation、读取资源限制、提升 sealed
selector、恢复本 operation 持有的 ingress，并回读最终 delivery revision。journal 保留迁移
门禁失败历史，并记录 forward-recovery 回执和新增 phase。之后部署协调器可继续处理该发行物
中分别规划的剩余目标。

Recovery authorization is one-shot. If the authorized command fails or its process
outcome is uncertain, the journal remains `RUNNING` or `RECOVERY_REQUIRED`; neither
state allows replay. Do not rerun `commit`, `apply`, or `resume`. Keep ingress paused,
preserve the receipt and journal, and perform a read-only audit before defining a
separate reviewed recovery action. This path does not install the release reconciler
timer.

恢复授权仅可执行一次。如果授权命令失败或进程结果不确定，journal 保持 `RUNNING` 或
`RECOVERY_REQUIRED`；这两种状态都不能重放。不得重跑 `commit`、`apply` 或 `resume`。保持
ingress 暂停并保留回执与 journal，先完成只读审计，再定义单独审核的恢复动作。此入口不会
安装发行协调 timer。
