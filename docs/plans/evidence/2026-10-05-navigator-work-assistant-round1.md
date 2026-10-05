# Navigator Work Assistant, round 1 closeout evidence

Status: **BASELINE_DELIVERED_WITH_GAPS**. Navigator feature PR #30 and release-fix PR #31 are merged. Final source `992be03cd195d5056cf66ac1a30983fe2fea7332` merged as `e64cfae28faf69a4bc4fd0edea0f4e9a0b00423b` at 2026-10-05 05:11:36 UTC. All five PR #31 source and exact-SHA post-merge workflows passed; the final post-merge results were observed at 05:20:23 UTC. At 2026-10-05 05:22:58 UTC, fetch and direct remote read-back both matched e64cfae; source 992be03 is its ancestor. NWA1-DELIVERY passes within its repository-delivery scope. Workspace packaging repair PR #73 and its seven post-merge jobs passed. Real-host checks and VM cutover remain open.

## Input baselines and paired feature/release delivery

| Repository | Input baseline SHA | Feature and release-fix delivery |
| --- | --- | --- |
| Navigator | `04e84f7b7943bbe0d60604ff65194904b37ad821` | Feature PR [#30](https://github.com/DoHorizon-AI/Cyrene-Navigator/pull/30), source `839f01331abfd4210e1d28b936b362ca3b6bd55d`, merge `a0d8d2f72ae27a27d3d192d07948af2743531542`; release PR [#31](https://github.com/DoHorizon-AI/Cyrene-Navigator/pull/31), source `992be03cd195d5056cf66ac1a30983fe2fea7332`, merge `e64cfae28faf69a4bc4fd0edea0f4e9a0b00423b` |
| Plugins Official | `96733779d187b13efcadcde95a6b6737fe102d25` | PR [#108](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/pull/108), merge `c7fcd163fecdfcfbe976d2ed8d2de8987d36796c`; post-merge public-ci, publish, and connection-release all SUCCESS |
| Client | `9c51a27540951589157926d1ee717d912ed22fc2` | PR [#16](https://github.com/DoHorizon-AI/Cyrene-Client/pull/16), merge `809d6279ace453c1591d22c7fd0b2d634b0817c9`; CI, MSIX, and Container App all SUCCESS |
| Workspace | `599bbe02a8d8b4050042c75c23c297d33021b8ea` | Feature PR [#69](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/69), merge `654cea321c23a649ed3303fcd430379bdb5713f0`; release PR [#73](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/73), source `4fd835f188c05493af2d319b5bf20fc141b09aa1`, merge `cda6fb5104373293575342b2e6868cdd98a3d626` |
| Exchange | `7f1823f6eabe9956ff40cc2e4aa9035197b51e83` | No repository change; existing model API reused |

Plugins post-merge runs public-ci 37258904025, publish 37258904022, and connection-release 37258904005 were SUCCESS; later develop tip `5c6879d8d3b13f3c3679da62e6d9f4d23475e750` contains merge c7fcd163. Client merge/read-back is `809d6279ace453c1591d22c7fd0b2d634b0817c9`. Workspace feature merge #69 was read back in develop; release merge #73 is in current live tip `93ae3180aab541b125a5c40675bca84dd06a23f8`, which also includes PR #72. Navigator #30 was read back as `a0d8d2f72ae27a27d3d192d07948af2743531542`; final PR #31 read-back is `e64cfae28faf69a4bc4fd0edea0f4e9a0b00423b`.

## Workspace release repair

Workspace PR #73 source `4fd835f188c05493af2d319b5bf20fc141b09aa1` merged as `cda6fb5104373293575342b2e6868cdd98a3d626`. Source CI run 37265692623 passed all six test jobs. Exact-merge post-merge run 37265863366 passed all seven jobs: integration-profile, component-updates, data-bundle-regression, nativeinstaller 22.04, nativeinstaller 24.04, lifecycle-integration, and immutable-artifact.

The release issue was overly strict producer/consumer tag matching for tzdata dual py2.py3 wheels. The repair accepts a wheel when at least one tag matches while retaining hash, filename, metadata, and ABI checks. Local verification passed 44 target tests, Ruff, and format check. Exact-source tzdata wheelhouse and bundle diagnostics passed after rebuilding canonical SDK source ba253. Synthetic provenance was only a local diagnostic, not a signed production release.

## Navigator merge evidence

PR #31 merged normally at 2026-10-05 05:11:36 UTC. Source `992be03cd195d5056cf66ac1a30983fe2fea7332` merged as `e64cfae28faf69a4bc4fd0edea0f4e9a0b00423b`. Fetch, origin/develop, and direct remote read-back all matched e64cfae; source 992be03 is an ancestor.

All five exact-source PR #31 workflows passed:

| Workflow | Run | Result |
| --- | --- | --- |
| Main CI | [37265968527](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37265968527) | SUCCESS |
| ARM64 | [37265968550](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37265968550) | SUCCESS |
| Windows | [37265968518](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37265968518) | SUCCESS |
| Immutable component | [37265968515](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37265968515) | SUCCESS; PR package publishing skipped |
| Product | [37265968388](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37265968388) | SUCCESS |

The exact-SHA post-merge workflows all passed; final verification was observed at 2026-10-05 05:20:23 UTC:

| Workflow | Exact-merge run | Status |
| --- | --- | --- |
| CI | [37266682622](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37266682622) | SUCCESS |
| Product | [37266682675](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37266682675) | SUCCESS |
| Windows | [37266682672](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37266682672) | SUCCESS |
| Component release | [37266682651](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37266682651) | SUCCESS; four jobs passed; signed Product Python publication verified for Ubuntu 22.04/24.04 x64 |
| ARM64 | [37266682683](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37266682683) | SUCCESS |

The earlier PR #30 component publication failed because wheelhouse tag matching rejected the tzdata dual-tag wheel. Workspace #73 fixes the wheelhouse logic; Navigator #31 pins both builder references to cda6fb5 and adds serial Windows fixture execution plus tool-result/event-failure diagnostics. The PR #31 component source check skipped package publishing. Exact-merge component run 37266682651 passed four jobs: Product tests, native builds for Ubuntu 22.04 and 24.04, and component release. The [preview release](https://github.com/DoHorizon-AI/Cyrene-Navigator/releases/tag/preview-e64cfae28faf69a4bc4fd0edea0f4e9a0b00423b) read-back verifies the index, manifests, artifacts, and attestations; the release is immutable=true with eight assets. Index raw SHA256 is `b983e1b2c426fb83e4437ddba8734a0cad195e7157c4cd780a827f8b6189ea20`. This signed publication covers Product Python packages for Ubuntu 22.04/24.04 x64, not Navigator Host releases for Windows or ARM64.

## Local verification and source boundaries

- **Node and native tests:** the seven-file suite passed 33/33 with zero skips; Windows Python passed 31/31, native tests 12/12, and imports 5/5. Python passed 84/84, the timezone subset under PYTHONTZPATH='' passed 19, and three OpenAPI validators passed.
- **Docker source:** a scoped comparison confirmed every Docker COPY production input is unchanged from `1eeb5bb37d839183850c700bf59df54eac75c41e` through source `992be03cd195d5056cf66ac1a30983fe2fea7332`, including the executor Dockerfile/ignore, README, Python lock/source, harness source/presets/package/pins/tsconfig, and four startup scripts.
- **Container smoke:** Linux/amd64 image SHA256 `fa9ea4ae1da31bdaea9920a5e5d5cdb491e1bff662d94806554bc226e22177ef` was built from source `1eeb5bb37d839183850c700bf59df54eac75c41e` with UID 10001. It was not rebuilt from source 839, 992, or e64c. HTTP status was 200; SQLite integrity_check was ok; a named-volume marker survived restart. No model request was made.
- **Exchange mock smoke:** Product source `a0ab494fc9e811d49f6fe6cd5006eeba1236dfe8` has the same Product tree as Exchange baseline `7f1823f6eabe9956ff40cc2e4aa9035197b51e83`. Local mock results were Control 201, unauthenticated request 401, model route 200, and upstream 429 mapped to 502. This was not an external model call.
- **Client browser:** local Playwright could not start Chromium because libnspr4.so was unavailable. Client hosted unit/build/MSIX/Container App jobs passed; no local browser E2E pass is claimed.
- **Local integration scope:** NWA1-FLOW exercised simulated normalized WeCom intake through the actual dsh loop into SQLite task/event state. NWA1-RECOVERY exercised local restart, SQLite durability, and task-event SSE recovery. These checks pass within their declared local scope.

## Implemented capability and real-use boundaries

Navigator provides authenticated task REST/SSE, durable task/event history, failure/cancel/restart handling, per-session serialization, and an Exchange HTTP adapter. Work adds correlated approval/human-input gates, operation receipts, structured memory/freshness, complete paginated source snapshots, content-addressed attachments, leased notification delivery with deduplication, connector state, and workflow memory.

WeCom provides a Python long-connection bot and official CLI with separate bot/human identities, trusted inbound routing, attachments, health/reconnect, task-result outbox, and tool.provider.v1 bridge. QQ provides normalized connector naming, OneBot compatibility identity, QR expiry/account confirmation, health, and official-package download/stage/cache-pointer behavior. Automatic install and Host startup are not implemented. Real QQ account/login/private/group traffic is separately NOT_RUN because no authorized production account/Host was available. The current native Host candidate boundary is Linux x64; three-platform CI does not expand supported runtime platforms. NapCat is not used.

Antigravity stream-json and CodeBuddy ACP run as dsh-native subagents and keep their vendor authentication/process boundaries. They are not Exchange adapters.

## Cloud profiles and workflow memory

Configured profiles include HF MCP, Azure MCP plus Learn documentation, Google Developer Knowledge documentation MCP, and fixed read-only gcloud operations. Credentials remain on the actual execution host. dsh Schedule is the only timer. Versioned workflow memory stores targets, evidence, permissions, and notification rules, with UTC as the default timezone. Evidence for a target/projectId is a successful tool receipt from that target. Unknown, unavailable, or missing-credential targets fail preflight. Scheduled HF patrol requires explicit per-target read-only classification and an available tool/provider implementation; an explicit HF MCP connection alone does not enable patrol. Write operations are guarded. Unchanged patrols stay quiet. Durable terminal-notification recovery retries the receipt without replaying task execution. These results come from fixtures, not real cloud operations.

## Stable acceptance checks

| Check ID | Evidence | Status |
| --- | --- | --- |
| NWA1-FLOW | Simulated normalized WeCom intake reaches the actual dsh loop and SQLite persistence. | [x] LOCAL_INTEGRATION_PASS within declared local scope |
| NWA1-RECOVERY | Local restart, SQLite persistence, and task-event SSE recovery. | [x] LOCAL_INTEGRATION_PASS within declared local scope |
| NWA1-CONNECTORS | WeCom/QQ protocol fixtures pass. | [ ] Real tenant/account/Host traffic is NOT_RUN |
| NWA1-HOST | No credentialed production-host operation. | [ ] Real model, WeCom, QQ, cloud, and target-host checks are NOT_RUN |
| NWA1-DELIVERY | PR #31 merged/read back; all five source and exact-merge workflows passed. | [x] Repository merge, canonical read-back, and exact-SHA CI/release acceptance passed; real-host checks remain separate. |

## Follow-up paths and remaining gaps

Real WeCom tenant/CLI traffic, QQ package installation and Host startup, authorized QQ account/login/private/group messages, Antigravity/CodeBuddy accounts, external Exchange model calls, credentialed cloud operations, and local Client browser E2E remain NOT_RUN. No VM was switched and no historical data was migrated; the old service and snapshots remain.

1. **Repair and acceptance:** fetch the latest target develop before work. Implement and verify QQ package installation/Host startup within the Linux x64 candidate boundary and then exercise an authorized real account; classify every HF patrol target read-only; close Client browser E2E; and separately review selected-data migration and real-host checks.
2. **Capability expansion:** migrate remaining generic Hermes tools, Hermes Skills, and DH-Skills through Work OpenAPI, message.connector.v1, tool.provider.v1, and dsh subagent/Schedule boundaries. Keep dsh as the sole executor and scheduler, and Navigator as the sole durable work store.

VM cutover requires separate evidence for real model/connector use, selected-data migration, deduplication under parallel operation, traffic switch, and rollback.

---
<!-- Chinese Translation / 中文翻译 -->

# Navigator 工作助手第一轮收尾证据

状态：**BASELINE_DELIVERED_WITH_GAPS**。Navigator 功能 PR #30 与发布修复 PR #31 均已合入。最终源提交 `992be03cd195d5056cf66ac1a30983fe2fea7332` 于 2026-10-05 05:11:36 UTC 以 `e64cfae28faf69a4bc4fd0edea0f4e9a0b00423b` 合入。PR #31 源提交的五个工作流和精确 SHA 的五个合入后工作流全部通过，最终合入后结果于 05:20:23 UTC 确认。2026-10-05 05:22:58 UTC 的 fetch 与直接远端读回均为 e64cfae，并确认源提交 992be03 是其祖先。按仓库交付范围，NWA1-DELIVERY 通过。Workspace 打包修复 PR #73 和七个合入后作业全部通过。真实宿主检查和 VM 切换仍未完成。

## 输入基线与配对功能/发布 PR

| 仓库 | 输入基线 SHA | 功能与发布修复交付 |
| --- | --- | --- |
| Navigator | `04e84f7b7943bbe0d60604ff65194904b37ad821` | 功能 PR [#30](https://github.com/DoHorizon-AI/Cyrene-Navigator/pull/30)，源提交 `839f01331abfd4210e1d28b936b362ca3b6bd55d`，合入 `a0d8d2f72ae27a27d3d192d07948af2743531542`；发布 PR [#31](https://github.com/DoHorizon-AI/Cyrene-Navigator/pull/31)，源提交 `992be03cd195d5056cf66ac1a30983fe2fea7332`，合入 `e64cfae28faf69a4bc4fd0edea0f4e9a0b00423b` |
| Plugins Official | `96733779d187b13efcadcde95a6b6737fe102d25` | PR [#108](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/pull/108)，合入 `c7fcd163fecdfcfbe976d2ed8d2de8987d36796c`；合入后 public-ci、publish、connection-release 全部 SUCCESS |
| Client | `9c51a27540951589157926d1ee717d912ed22fc2` | PR [#16](https://github.com/DoHorizon-AI/Cyrene-Client/pull/16)，合入 `809d6279ace453c1591d22c7fd0b2d634b0817c9`；CI、MSIX、Container App 全部 SUCCESS |
| Workspace | `599bbe02a8d8b4050042c75c23c297d33021b8ea` | 功能 PR [#69](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/69)，合入 `654cea321c23a649ed3303fcd430379bdb5713f0`；发布 PR [#73](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/73)，源提交 `4fd835f188c05493af2d319b5bf20fc141b09aa1`，合入 `cda6fb5104373293575342b2e6868cdd98a3d626` |
| Exchange | `7f1823f6eabe9956ff40cc2e4aa9035197b51e83` | 仓库未修改；复用现有模型 API |

Plugins 合入后运行 public-ci 37258904025、publish 37258904022、connection-release 37258904005 全部 SUCCESS；之后 develop tip `5c6879d8d3b13f3c3679da62e6d9f4d23475e750` 包含合入 c7fcd163。Client 合入/读回为 `809d6279ace453c1591d22c7fd0b2d634b0817c9`。Workspace 功能 PR #69 已在 develop 读回；发布 PR #73 位于当前 live tip `93ae3180aab541b125a5c40675bca84dd06a23f8` 中，该 tip 也包含 PR #72。Navigator #30 读回为 `a0d8d2f72ae27a27d3d192d07948af2743531542`；PR #31 最终读回为 `e64cfae28faf69a4bc4fd0edea0f4e9a0b00423b`。

## Workspace 发布修复

Workspace PR #73 源提交 `4fd835f188c05493af2d319b5bf20fc141b09aa1` 以 `cda6fb5104373293575342b2e6868cdd98a3d626` 合入。源 CI run 37265692623 的六个测试作业通过。精确合入后 run 37265863366 的七个作业全部通过：integration-profile、component-updates、data-bundle-regression、nativeinstaller 22.04、nativeinstaller 24.04、lifecycle-integration 和 immutable-artifact。

发布问题是 producer/consumer 对 tzdata dual py2.py3 wheels 的 tag 匹配过严。修复允许至少一个 tag 匹配，同时保留 hash、文件名、metadata 和 ABI 检查。本地 44 个 target 测试、Ruff 和 format check 通过。以 canonical SDK 源码 ba253 重建后，精确来源 wheelhouse 和 bundle 诊断通过。合成 provenance 仅是本地诊断，不是签名生产发布。

## Navigator 合入证据

PR #31 于 2026-10-05 05:11:36 UTC 正常合入。源提交 `992be03cd195d5056cf66ac1a30983fe2fea7332` 以 `e64cfae28faf69a4bc4fd0edea0f4e9a0b00423b` 合入。fetch、origin/develop 和直接远端读回均为 e64cfae；源提交 992be03 是其祖先。

PR #31 精确源 SHA 的五项工作流全部通过：

| 工作流 | Run | 结果 |
| --- | --- | --- |
| Main CI | [37265968527](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37265968527) | SUCCESS |
| ARM64 | [37265968550](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37265968550) | SUCCESS |
| Windows | [37265968518](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37265968518) | SUCCESS |
| Immutable component | [37265968515](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37265968515) | SUCCESS；PR 包发布已跳过 |
| Product | [37265968388](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37265968388) | SUCCESS |

精确合入 SHA 的五个工作流全部通过；最后一次核验时间为 2026-10-05 05:20:23 UTC：

| 工作流 | 精确合入 Run | 状态 |
| --- | --- | --- |
| CI | [37266682622](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37266682622) | SUCCESS |
| Product | [37266682675](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37266682675) | SUCCESS |
| Windows | [37266682672](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37266682672) | SUCCESS |
| Component release | [37266682651](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37266682651) | SUCCESS；四个作业通过；已验证 Ubuntu 22.04/24.04 x64 签名 Product Python 发布 |
| ARM64 | [37266682683](https://github.com/DoHorizon-AI/Cyrene-Navigator/actions/runs/37266682683) | SUCCESS |

PR #30 的 component 发布曾因 wheelhouse 对 tzdata 双标签 wheel 的匹配过严而失败。Workspace #73 修复 wheelhouse 逻辑；Navigator #31 将两个 builder 引用固定到 cda6fb5，并增加 Windows 夹具串行执行及 tool-result/event-failure diagnostics。PR #31 源提交的 component 检查跳过了包发布。精确合入 component run 37266682651 的四个作业通过：Product tests、Ubuntu 22.04/24.04 原生构建和 component release。[Preview release](https://github.com/DoHorizon-AI/Cyrene-Navigator/releases/tag/preview-e64cfae28faf69a4bc4fd0edea0f4e9a0b00423b) 读回验证 index、manifests、artifacts 和 attestations；发布为 immutable=true，共八个资产。Index 原始 SHA256 为 `b983e1b2c426fb83e4437ddba8734a0cad195e7157c4cd780a827f8b6189ea20`。该签名发布仅覆盖 Ubuntu 22.04/24.04 x64 的 Product Python 包，不代表 Windows 或 ARM64 Navigator Host 发布。

## 本地验证与来源边界

- **Node 和 native 测试：** 七文件套件通过 33/33、零跳过；Windows Python 31/31、native 12/12、imports 5/5。Python 84/84 通过；PYTHONTZPATH='' 下时区子集 19 项通过；三个 OpenAPI 校验器通过。
- **Docker 来源：** 限定范围的比较确认，从 `1eeb5bb37d839183850c700bf59df54eac75c41e` 到源提交 `992be03cd195d5056cf66ac1a30983fe2fea7332`，所有 Docker COPY 生产输入均未变化，包括 executor Dockerfile/ignore、README、Python lock/source、harness source/presets/package/pins/tsconfig 和四个启动脚本。
- **容器烟测：** Linux/amd64 镜像 SHA256 `fa9ea4ae1da31bdaea9920a5e5d5cdb491e1bff662d94806554bc226e22177ef` 由源提交 `1eeb5bb37d839183850c700bf59df54eac75c41e` 构建，运行 UID 10001。镜像未从 839、992 或 e64c 重建。HTTP 状态为 200；SQLite integrity_check 为 ok；命名卷标记在重启后保留。未发起模型请求。
- **Exchange 模拟烟测：** Product 源码 `a0ab494fc9e811d49f6fe6cd5006eeba1236dfe8` 与 Exchange 基线 `7f1823f6eabe9956ff40cc2e4aa9035197b51e83` 的 Product 树相同。本地模拟结果为 Control 201、未认证请求 401、模型路由 200、上游 429 映射为 502。这不是真实外部模型调用。
- **Client 浏览器：** 本地 Playwright 因缺少 libnspr4.so 无法启动 Chromium。Client 托管 unit/build/MSIX/Container App 作业通过；没有声称本地浏览器 E2E 通过。
- **本地集成范围：** NWA1-FLOW 验证模拟规范化 WeCom 入站经过实际 dsh loop 并写入 SQLite 任务/事件。NWA1-RECOVERY 验证本地重启、SQLite 持久性和任务事件 SSE 恢复。这两项在声明的本地范围内通过。

## 已实现能力与真实使用边界

Navigator 提供认证任务 REST/SSE、持久任务/事件历史、失败/取消/重启处理、按会话串行化和 Exchange HTTP 适配器。Work 增加关联审批/人工输入门、操作回执、结构化记忆/新鲜度、完整分页来源快照、内容寻址附件、带租约和去重的通知投递、连接器状态与工作流记忆。

WeCom 提供 Python 长连接机器人和官方 CLI，隔离机器人/真人身份，并支持可信入站路由、附件、健康/重连、任务结果 outbox 和 tool.provider.v1 桥接。QQ 提供规范化连接器命名、OneBot 兼容标识、二维码时效/账号确认、健康状态和官方包下载/暂存/缓存指针。自动安装和 Host 启动尚未实现；由于没有获授权的生产账号/Host，真实 QQ 账号/登录/私聊/群聊流量另记为 NOT_RUN。当前原生 Host 候选运行边界是 Linux x64；三平台 CI 不会扩大运行平台支持范围。不使用 NapCat。

Antigravity stream-json 和 CodeBuddy ACP 作为 dsh 原生子代理运行，并保留各自厂商认证/进程边界。它们不是 Exchange 适配器。

## 云连接配置与工作流记忆

已配置的 profile 包括 HF MCP、Azure MCP 与 Learn 文档、Google Developer Knowledge 文档 MCP，以及固定只读 gcloud 操作。凭据保留在实际执行宿主。dsh Schedule 是唯一的定时器。版本化工作流记忆记录目标、证据、权限和通知规则，默认时区为 UTC。target/projectId 的验收证据应来自该目标成功执行工具后的回执。未知、不可用或缺少凭据的目标会被预检拒绝。定时 HF 巡检需要逐目标明确只读分类，并且工具/服务提供方实现可用；显式连接 HF MCP 本身不会启用定时巡检。写操作受 guard 保护。巡检无变化时保持安静。持久终态通知恢复会重试回执，不会重放任务执行。这些结果来自夹具，不代表真实云操作。

## 稳定验收检查

| 检查 ID | 证据 | 状态 |
| --- | --- | --- |
| NWA1-FLOW | 模拟规范化 WeCom 入站到达实际 dsh loop 和 SQLite 持久化。 | [x] 在声明的本地范围内 LOCAL_INTEGRATION_PASS |
| NWA1-RECOVERY | 本地重启、SQLite 持久性和任务事件 SSE 恢复。 | [x] 在声明的本地范围内 LOCAL_INTEGRATION_PASS |
| NWA1-CONNECTORS | WeCom/QQ 协议夹具通过。 | [ ] 真实租户/账号/Host 消息流量为 NOT_RUN |
| NWA1-HOST | 未执行带凭据生产宿主操作。 | [ ] 真实模型、WeCom、QQ、云和目标宿主检查为 NOT_RUN |
| NWA1-DELIVERY | PR #31 已合入并读回；五项源工作流和精确合入后工作流全部通过。 | [x] 仓库合入、规范远端读回及精确 SHA CI/发布验收通过；真实宿主检查另行追踪。 |

## 后续路径与剩余缺口

真实 WeCom 租户/CLI 流量、QQ 软件包安装与 Host 启动、获授权的 QQ 登录/私聊/群聊、Antigravity/CodeBuddy 账号、外部 Exchange 模型调用、带凭据云操作及本地 Client 浏览器 E2E 仍为 NOT_RUN。未切换 VM，也未迁移历史数据；旧服务和快照仍保留。

1. **修问题并验收：** 开始前 fetch 最新目标 develop。在 Linux x64 候选边界内实现并验证 QQ 包安装/Host 启动，再用获授权真实账号验收；逐项将 HF 巡检目标分类为只读；关闭 Client 浏览器 E2E；并单独评审选定数据迁移和真实宿主检查。
2. **继续扩展能力：** 通过 Work OpenAPI、message.connector.v1、tool.provider.v1 和 dsh subagent/Schedule 边界迁移剩余 Hermes 通用工具、Hermes Skills 和 DH-Skills。保持 dsh 是唯一执行核心和调度器，Navigator 是唯一持久工作存储。

VM 切换需要单独提供真实模型/连接器使用、选定数据迁移、并行去重、流量切换和回滚证据。
