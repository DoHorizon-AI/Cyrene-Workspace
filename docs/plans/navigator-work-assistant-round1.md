# Navigator Work Assistant migration, round 1

Status: **BASELINE_DELIVERED_WITH_GAPS**. Navigator feature PR #30 merged source `839f01331abfd4210e1d28b936b362ca3b6bd55d` as `a0d8d2f72ae27a27d3d192d07948af2743531542`; release-fix PR #31 merged source `992be03cd195d5056cf66ac1a30983fe2fea7332` as `e64cfae28faf69a4bc4fd0edea0f4e9a0b00423b`. At 2026-10-05 05:22:58 UTC, fetched origin/develop and direct remote read-back matched e64cfae; source 992be03 is its ancestor. All five source and post-merge workflows passed, with final post-merge results observed at 05:20:23 UTC. The immutable signed release covers Product Python packages for Ubuntu 22.04/24.04 x64 only. Workspace PR #73 also merged and all seven post-merge jobs passed. Real-host acceptance and VM cutover remain open.

DeepSeek Harness (dsh) is the only top-level executor and timer authority. Exchange serves ordinary model API requests. Antigravity and CodeBuddy keep their native vendor authentication and processes as dsh subagents, not Exchange adapters. Navigator owns durable tasks/events, approvals and human input, operation receipts, structured memory/freshness, attachments, connector state, and notification deduplication.

Round 1 implements authenticated task REST/SSE, durable task/event history, failure/cancel/restart handling, per-session serialization, and an Exchange HTTP adapter. The Work domain adds correlated approval and human-input gates, operation receipts, complete paginated source snapshots, content-addressed attachments, a leased notification outbox with deduplication, and workflow memory.

WeCom enters through a Python long-connection bot and official CLI with separate bot and authorized-human identities. The product is named QQ connector; OneBot remains a compatibility protocol. The current QQ flow downloads official packages to staging/cache and records a cache pointer. Automatic package installation and Host startup are not implemented. This is a code gap, separate from real-account acceptance, which is NOT_RUN because no authorized production account/Host was available. The current native QQ Host candidate boundary is Linux x64; ARM64 and Windows CI do not expand supported runtime platforms. No NapCat dependency is allowed. The paired Client uses Navigator Control and exposes task/SSE, approvals, memory, notifications, attachments, connector state, and QR display.

## Delivery and evidence

The five input baselines, paired feature/release PRs, exact merge SHAs, source workflows, and bounded local evidence are listed in the [round 1 closeout report](evidence/2026-10-05-navigator-work-assistant-round1.md). Workspace PR #69 and PR #73 deliver the feature baseline and wheelhouse repair; Navigator PR #30 and PR #31 deliver the feature baseline and release repair. Plugins PR #108 and Client PR #16 are merged. Exchange had no repository changes in this round.

Workspace #73 fixes tzdata dual py2.py3 wheel tag matching while retaining hash, filename, metadata, and ABI checks. Local tests and diagnostics passed; its exact merge post-CI passed all seven jobs. Synthetic provenance was only a local diagnostic and is not a signed production release.

Navigator #31 fixes the two Workspace builder pins, serializes the seven-file Windows suite, and improves tool-result/event-failure diagnostics. All five source workflows passed. All five exact-SHA post-merge workflows passed, including signed component publication. NWA1-DELIVERY is complete within its declared repository-delivery scope.

The earlier Linux/amd64 container image was built from source `1eeb5bb37d839183850c700bf59df54eac75c41e`, not from 839 or 992. A scoped comparison confirms the Docker COPY production inputs remain unchanged through 992. No later image rebuild is claimed.

## Acceptance checks

Check IDs are stable. NWA1-FLOW and NWA1-RECOVERY are accepted within their declared local integration scope; real tenant and production checks remain separate.

| Check ID | Evidence and scope | Status |
| --- | --- | --- |
| NWA1-FLOW | LOCAL_INTEGRATION_PASS: simulated normalized WeCom intake reaches the actual dsh loop and SQLite task/event persistence. | [x] Local integration scope passed; real connector traffic is tracked by NWA1-HOST. |
| NWA1-RECOVERY | LOCAL_INTEGRATION_PASS: local process restart, SQLite persistence, and task-event SSE recovery. | [x] Local integration scope passed; production and migrated-database recovery remain NOT_RUN. |
| NWA1-CONNECTORS | WeCom and QQ protocol fixtures pass. | [ ] Real WeCom tenant traffic and authorized QQ Host/account/message checks remain NOT_RUN. |
| NWA1-HOST | No authorized production-host acceptance has run. | [ ] Real Exchange model call, WeCom/QQ, credentialed cloud checks, and target-host acceptance remain NOT_RUN. |
| NWA1-DELIVERY | Feature/release PRs merged and read back; all five PR #31 source and post-merge workflows passed. | [x] Repository delivery passed; real connector/host acceptance remains under NWA1-CONNECTORS and NWA1-HOST. |

## Cloud profiles and workflow memory

Configured cloud profiles include HF MCP, Azure MCP and Learn documentation, Google Developer Knowledge documentation MCP, and fixed read-only gcloud operations. Credentials stay on the actual execution host. dsh Schedule is the only timer authority. Versioned workflow memory records targets, evidence, permissions, and notification rules; the default timezone is UTC. Evidence for a target/projectId is a successful tool receipt from that target. Unknown, unavailable, or missing-credential targets fail preflight. Scheduled HF patrol requires explicit per-target read-only classification and an available tool/provider implementation; an explicit HF MCP connection alone does not enable patrol. Write operations are guarded. Unchanged patrols stay quiet. Durable terminal-notification recovery retries the receipt without replaying task execution. These are fixture results, not real cloud operations.

## Remaining gaps and next work

Real WeCom tenant/CLI traffic, QQ package installation and Host startup, QQ production login/private/group messages, Antigravity and CodeBuddy accounts, external Exchange model calls, credentialed cloud operations, and local Client browser E2E remain NOT_RUN. Chromium cannot start in the local Playwright environment because libnspr4.so is missing. No VM was switched and no historical data was migrated; the old service and snapshots remain. Hermes engine, gateway, model adapters, unmigrated generic tools, Hermes Skills, DH-Skills, and compatibility code remain outside this round.

Two follow-up paths remain:

1. **Repair and acceptance:** fetch the latest target develop before work. Implement and verify QQ package installation/Host startup within the Linux x64 candidate boundary, then exercise an authorized real account; classify each HF patrol target read-only; close Client browser E2E; and separately review selected-data migration and real-host checks.
2. **Capability expansion:** migrate remaining generic Hermes tools, Hermes Skills, and DH-Skills through Work OpenAPI, message.connector.v1, tool.provider.v1, and dsh subagent/Schedule boundaries. Keep dsh as the sole executor and scheduler, and Navigator as the sole durable work store.

A VM cutover requires separate evidence for real model/connector use, selected-data migration, deduplication under parallel operation, traffic switch, and rollback.

---
<!-- Chinese Translation / 中文翻译 -->

# Navigator 工作助手迁移第一轮

状态：**BASELINE_DELIVERED_WITH_GAPS**。Navigator 功能 PR #30 将源提交 `839f01331abfd4210e1d28b936b362ca3b6bd55d` 以 `a0d8d2f72ae27a27d3d192d07948af2743531542` 合入；发布修复 PR #31 将源提交 `992be03cd195d5056cf66ac1a30983fe2fea7332` 以 `e64cfae28faf69a4bc4fd0edea0f4e9a0b00423b` 合入。2026-10-05 05:22:58 UTC 的 fetch 与直接远端读回均为 e64cfae，并确认源提交 992be03 是其祖先。PR #31 五个源工作流和五个合入后工作流全部通过；合入后工作流最终结果于 05:20:23 UTC 确认。不可变签名发布仅覆盖 Ubuntu 22.04/24.04 x64 的 Product Python 包。Workspace PR #73 也已合入，七个合入后作业全部通过。真实宿主验收和 VM 切换仍未完成。

DeepSeek Harness（dsh）是唯一的顶层执行核心和定时权威。Exchange 提供普通模型 API。Antigravity 和 CodeBuddy 保留原生厂商认证与进程，作为 dsh 子代理运行，而不是 Exchange 适配器。Navigator 负责持久任务/事件、审批与人工输入、操作回执、结构化记忆/新鲜度、附件、连接器状态和通知去重。

第一轮实现认证任务 REST/SSE、持久任务/事件历史、失败/取消/重启处理、按会话串行化和 Exchange HTTP 适配器。Work 域增加关联审批和人工输入门、操作回执、完整分页来源快照、内容寻址附件、带租约和去重的通知 outbox，以及工作流记忆。

WeCom 通过 Python 长连接机器人和官方 CLI 接入，机器人与获授权真人身份相互隔离。产品名称为 QQ 连接器；OneBot 保留为兼容协议。当前 QQ 流程只下载官方包至暂存/缓存并记录缓存指针；自动安装软件包和启动 Host 尚未实现。这属于代码缺口，与真实账号验收分开记录；由于没有获授权的生产账号/Host，真实账号检查为 NOT_RUN。当前原生 QQ Host 候选运行边界是 Linux x64；ARM64 和 Windows CI 不会扩大已支持的平台。不依赖 NapCat。配对 Client 通过 Navigator Control 接入，提供任务/SSE、审批、记忆、通知、附件、连接器状态和二维码展示。

## 交付与证据

五个输入基线、功能/发布修复 PR、精确合入 SHA、源工作流和有限范围的本地证据均见[第一轮收尾证据报告](evidence/2026-10-05-navigator-work-assistant-round1.md)。Workspace PR #69/#73 分别交付功能基线和 wheelhouse 修复；Navigator PR #30/#31 分别交付功能基线和发布修复。Plugins PR #108 和 Client PR #16 已合入。本轮 Exchange 仓库未改动。

Workspace #73 修复 tzdata dual py2.py3 wheel 的 tag 匹配，同时保留 hash、文件名、metadata 和 ABI 检查。本地测试与诊断通过；精确合入 SHA 的七个合入后作业全部通过。合成 provenance 只是本地诊断，不是签名生产发布。

Navigator #31 修复两个 Workspace builder pin，串行运行七文件 Windows 套件，并增强 tool-result/event-failure diagnostics。五个源工作流全部通过。精确 SHA 的五个合入后工作流全部通过，包含签名 component 发布。按声明的仓库交付范围，NWA1-DELIVERY 已完成。

此前 Linux/amd64 容器镜像由源提交 `1eeb5bb37d839183850c700bf59df54eac75c41e` 构建，并非由 839 或 992 构建。限定范围的比较确认 Docker COPY 生产输入直到 992 均未变化。本轮没有声称重新构建镜像。

## 验收检查

检查 ID 保持稳定。NWA1-FLOW 和 NWA1-RECOVERY 按声明的本地集成范围验收；真实租户和生产环境检查另行追踪。

| 检查 ID | 证据与范围 | 状态 |
| --- | --- | --- |
| NWA1-FLOW | LOCAL_INTEGRATION_PASS：模拟规范化 WeCom 入站到达实际 dsh loop，并写入 SQLite 任务/事件。 | [x] 本地集成范围通过；真实连接器流量归入 NWA1-HOST。 |
| NWA1-RECOVERY | LOCAL_INTEGRATION_PASS：本地进程重启、SQLite 持久性和任务事件 SSE 恢复。 | [x] 本地集成范围通过；生产和迁移数据库恢复仍为 NOT_RUN。 |
| NWA1-CONNECTORS | WeCom 和 QQ 协议夹具通过。 | [ ] 真实 WeCom 租户流量及获授权 QQ Host/账号/消息检查仍为 NOT_RUN。 |
| NWA1-HOST | 尚未执行获授权的生产宿主验收。 | [ ] 真实 Exchange 模型调用、WeCom/QQ、带凭据云检查和目标宿主验收仍为 NOT_RUN。 |
| NWA1-DELIVERY | 功能/发布修复 PR 已合入并读回；PR #31 五个源及合入后工作流全部通过。 | [x] 仓库交付通过；真实连接器/宿主验收仍由 NWA1-CONNECTORS 与 NWA1-HOST 跟踪。 |

## 云连接配置与工作流记忆

当前配置包括 HF MCP、Azure MCP 与 Learn 文档、Google Developer Knowledge 文档 MCP，以及固定只读的 gcloud 操作。凭据留在实际执行宿主。dsh Schedule 是唯一的定时权威。版本化工作流记忆保存目标、证据、权限和通知规则，默认时区为 UTC。某个 target/projectId 的证据来自该目标成功执行工具后的回执。未知、不可用或缺少凭据的目标会被预检拒绝。定时 HF 巡检需要逐目标明确只读分类，并且工具/服务提供方实现可用；显式连接 HF MCP 本身不会启用定时巡检。写操作受 guard 保护。巡检无变化时保持安静。持久终态通知恢复时会重试回执，不会重放任务执行。这些结果来自夹具，不代表真实云操作。

## 尚存缺口与后续工作

真实 WeCom 租户/CLI 流量、QQ 软件包安装和 Host 启动、QQ 生产登录/私聊/群聊、Antigravity 和 CodeBuddy 账号、外部 Exchange 模型调用、带凭据云操作及本地 Client 浏览器 E2E 均为 NOT_RUN。当前 Playwright 环境缺少 libnspr4.so，Chromium 无法启动。本轮未切换 VM，也未迁移历史数据；旧服务和快照仍保留。Hermes 引擎、网关、模型适配器、未迁通用工具、Hermes Skills、DH-Skills 和兼容代码仍不在本轮范围内。

后续工作分两条路径：

1. **修问题并验收：** 开始工作前 fetch 最新目标 develop。在 Linux x64 候选运行边界内实现并验证 QQ 包安装/Host 启动，再使用获授权真实账号验收；逐项将 HF 巡检目标归类为只读；补齐 Client 浏览器 E2E；单独评审选定数据迁移和真实宿主检查。
2. **继续扩展能力：** 通过 Work OpenAPI、message.connector.v1、tool.provider.v1 和 dsh subagent/Schedule 边界迁移剩余 Hermes 通用工具、Hermes Skills 与 DH-Skills。dsh 继续作为唯一执行核心和调度器，Navigator 继续作为唯一持久工作存储。

VM 切换需要单独取得真实模型/连接器使用、选定数据迁移、并行去重、流量切换与回滚的证据。
