# Connection V2 and run recovery / 连接 V2 与任务恢复

This record covers the Authority V2 integration and the first Client run-recovery increment. Source checks, hosted CI, signed publication and deployed acceptance are distinct results. Earlier dual-machine results do not validate the new Authority V2 process.

本记录覆盖 Authority V2 集成与 Client 首批任务恢复功能。源码检查、托管 CI、签名发布和部署验收分别记录；此前的双机结果不能证明新版 Authority V2 进程已通过验收。

## Implemented boundaries / 已实现的边界

- Platform owns the independent Authority host, durable PostgreSQL outbox, identity and Directory checks, policy, revocation fences and approved execution-target bindings.
- Plugins owns Relay, Connector, Bridge, Sidecar and the lightweight SDK. Relay/Bridge carry the end-to-end mTLS stream. Connector persists its dispatch receipt; interrupted dispatch yields an unknown result instead of repeating the HTTP call.
- BFF selects the exact immutable artifact returned by Authority. Its build no longer fetches Product catalogs.
- Binary compatibility locks only generic protocol definitions. Product catalogs and Platform policy are separately attested data inputs; changing their content does not change the protocol lock.
- Linux activates verified data through protected Authority administration with a durable plan and monotonic generation. Windows coordinates the supported OCI group and binds each plan to the active Authority artifact.

中文：Platform 独立宿主保留身份、Directory、策略、撤销检查、持久调用队列与受信执行目标绑定。Plugins 管理传输和 Connector，分发中断后保留未知结果。BFF 根据 Authority 返回的精确产物读取目录，构建不再拉取 Product 契约。兼容锁只固定通用协议，Product 目录和策略分别提供来源证明。Linux 数据激活和 Windows OCI 更新均绑定持久计划与具体产物。

## Client increment / Client 本批功能

An accepted draft launch opens its run. An explicit retry first reads the durable draft/run association; an uncertain launch retains its command key. URL restoration, authenticated reads, bounded event replay, reconnect cursors and session cleanup support observation after a refresh or network interruption. Observation never resumes training automatically.

With `VITE_WORKSPACE_BFF_ENABLED=true`, users discover an authorized Workspace, read a prepared draft and explicitly start it. Run observation uses generic V2 calls. Yield owns `workspaceGetRun`, `workspaceListRunEvents` and `workspaceListRunAttempts`; Platform adds their policy data. Results bind organization, Workspace and run ID, and terminal event pages are drained before observation stops.

中文：草稿启动后进入接受的任务，重试先读取持久任务关联，结果不确定时保留原命令键。URL 恢复、认证读取、有界事件重放、游标续读和会话清理支持刷新与断网后的观察，不自动恢复训练。远程模式选择已授权 Workspace，读取准备好的草稿后明确启动；三项新增读取操作由 Yield 维护，Platform 增加策略数据。响应绑定组织、Workspace 和任务，终态仍读完事件页。

## Fixed inputs / 固定输入

| Input / 输入 | Source / 来源 |
| --- | --- |
| Generic protocol lock | Workspace `c7dea28958a97ccab3a9cc3199faf0ac5819a2a5`; SHA256 `00fd59fb76d7144b6e1b554feadf035328b216231e0a323178836abf2b8bdd5a` |
| Catalog generation 5 | Workspace `7e6d410017420de72d7688859ae81d675e4555ea`; SHA256 `f12f5cd1243d16b6ec6a5194efacbdf7a8bf9dffcf8d9525c35183c45a7c7816` |
| Client implementation | `e01b90a0e9c62324566198540654bee0fd0b8293` |

The protocol lock is `governance/workspace-connection-protocols-v2.lock.json`. Owner source pins are recorded in Platform's `tooling/workspace-product-contract-bundle/releases/workspace-product-v2.lock.json`. A source pin is not a successful signed release: the data publisher requires actual owner and policy release attestations for the supplied exact commits.

中文：协议锁路径见上。各 Product 来源固定于 Platform 的数据锁；源码提交不等于签名发布，数据发布器要求对应精确提交的真实 owner 和策略证明。

## Local evidence / 本地证据

| Area / 范围 | Observed checks / 已执行检查 | Limit / 限制 |
| --- | --- | --- |
| SDK and Sidecar | Locked offline normal-dependency trees contain no SQLx, PostgreSQL, WebAuthn or Platform server crates. | Sidecar retains its local IPC implementation. / 保留自身本机 IPC。 |
| Client Web | 51 focused tests, TypeScript, and builds with remote mode disabled/enabled passed. | New browser-to-remote-GPU flow unrun. / 新界面到远程 GPU 的完整流程未执行。 |
| Yield | Private API, contracts, durable event pagination/restart and type/lint checks passed. | These are scoped fixtures. / 验证使用指定范围的测试环境。 |
| Authority/storage/BFF | Host 3 tests, Trust 5 tests, snapshot 4 tests; locked checks/build and strict Clippy passed. | Full Authority V2 process acceptance unrun. / 完整新版进程验收未执行。 |
| PostgreSQL | Isolated PostgreSQL 17 migrations, TLS verify-full and SQL uniqueness race passed. | SQL race does not prove Rust enqueue/ACK/revocation races. / 尚未证明 Rust API 的相关并发行为。 |
| Linux updater | 66 focused updater/CLI tests, lint, compilation and schema checks passed. | Signed-artifact activation against real Authority pending. / 签名产物的真实 Authority 激活待验收。 |
| Windows installer | 30 tests, check, formatting and strict Clippy passed. | Windows/Docker Desktop runtime unrun; no Windows data-bundle updater. / 尚无实机验收和数据包更新器。 |

## Delivery and remaining work / 交付与剩余工作

Six Product source commits and Client were pushed to `develop` without rewriting history. Integration is carried by Workspace [PR #29](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/29), Platform [PR #71](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/71) and Plugins [PR #97](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/pull/97). Read current CI and merge status from GitHub; pending checks or merges are not recorded as successful here.

中文：六个 Product 与 Client 已正常推送 develop。上述三个 PR 承载集成，最新 CI 和合并结果从 GitHub 读回，本文不将等待中的检查或合并记为成功。

- Immutable release settings await the previously requested explicit authorization; publishers also require a dedicated settings-read credential. Six exact-head Product contract and component test jobs passed, but owner publication stopped at the missing `CYRENE_IMMUTABLE_RELEASES_READ_TOKEN` preflight, before querying the settings. Component publication separately stopped because no trusted immutable Platform preview index included the Runtime Maintenance SDK. These results are source-test evidence, not signed releases. No user token was copied into repository secrets.
- Real Authority V2 acceptance requires a signed owner/policy archive, protected import metadata, migrated restricted database roles, Entra verification and mTLS identities.
- Windows first startup requires a separately provisioned verified data bundle. This release updates supported OCI services but does not download or activate a new Windows data bundle.
- The remote UI uses an operator-approved Workspace target binding. Dynamic machine selection, remote parameter editing, cancellation, checkpoint resume, artifact download and Reactor deployment remain subsequent workflow increments.
- Network evidence remains limited to the agreed Tailscale/Cyrene Relay route. No new Azure resources or ACA production-deployment evidence are included.

中文：不可变设置仍待先前提出的明确授权，并需专用只读凭据。六个 Product 当前提交的契约和组件测试均通过；owner 发布因缺少专用只读 token 在查询设置前退出，组件发布另缺包含 Runtime Maintenance SDK 的受信 Platform 预览索引。这些结果不等于签名发布完成。真实验收还需签名数据包、受保护的导入记录、受限数据库迁移、Entra 和 mTLS 身份。Windows 仍需预置验证后的数据包。远程界面当前采用运维批准的目标绑定；动态选机及远程编辑、取消、检查点恢复、产物下载和 Reactor 部署属于后续增量。网络证据限定于既有 Tailscale/Cyrene Relay，不涉及新 Azure 资源或 ACA 生产部署。
