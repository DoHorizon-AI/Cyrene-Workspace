# ADR: Modular distribution preview / 模块化发行预览

Status (Stage71 source correction): The previously published immutable Native preview W `670dcc640c7716484edacf5c0f77dd7ec93b5fea` (18 assets, bound to the previously verified Platform release P `64e4b58eb1dedc5ea328c07c9b07df55d2f38590`) remains unchanged. On Host 2238, the exact Stage70 Catalyst plan passed staging, but `BeginCoreBootstrap` was rejected with `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`; the original transaction and guest state were preserved, and runtime source activation did not begin. Platform PR #103 is merged at `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4`; its focused tests passed 61 total (50 library and 11 binary). The natural official release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) is pending publication and strict asset verification at this checkpoint. Do not retry the old W/P pair or describe installation as successful. Client/browser, Plugin invocation, SFT, Echo, reboot, and clean-host acceptance remain **PENDING / NOT_RUN**.

状态（Stage71 源码修正）：此前发布的不可变 Native 预览版 W `670dcc640c7716484edacf5c0f77dd7ec93b5fea`（18 项资产，绑定此前已验证的 Platform release P `64e4b58eb1dedc5ea328c07c9b07df55d2f38590`）保持原样。Host 2238 上的 Stage70 Catalyst 精确计划已通过 staging，但 `BeginCoreBootstrap` 返回 `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`；原 transaction 与 guest 状态均已保留，runtime source activation 尚未开始。Platform PR #103 已正常合并至 `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4`；相关定向测试共 61 项通过（library 50 项、binary 11 项）。截至本文记录时，正式 release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) 尚待发布和严格资产验证。不要重试旧 W/P 发行对，也不要将安装描述为成功。Client/browser、Plugin invoke、SFT、Echo、重启和全新主机验收仍为 **PENDING / NOT_RUN**。


## Decision / 决策

Workspace owns workload selection, signed Catalog resolution, Product and
Plugin release discovery, and the check/stage/apply plan. Platform provides
generic package, maintenance, Broker, and activation mechanisms; product
composition stays out of Platform Core.

Workspace 负责 workload 选择、签名 Catalog 解析、Product 与 Plugin 发行物发现，以及 check/stage/apply plan。Platform 提供通用包管理、维护事务、Broker 和激活机制；产品组合不进入 Platform Core。

Catalyst is a native workload. Its required service, Client Control/Web, and
Runtime Maintenance SDK are resolved with four default-recommended data
plugins: Dataset Preparation, Document Parsing, Dataset Generation, and
Knowledge Preparation. Each plugin remains independently selectable through
the signed Catalog and `plugins` workload. Echo is a separate optional OCI
workload with its evaluator; it is not a Catalyst dependency. The signed
Catalog defaults to `stable`; this prerelease explicitly selects `preview`.

Catalyst 是原生 workload。其必需服务、Client Control/Web 和 Runtime Maintenance SDK 会与四个默认推荐的数据插件共同解析：Dataset Preparation、Document Parsing、Dataset Generation 和 Knowledge Preparation。每个插件仍可通过签名 Catalog 与 `plugins` workload 单独选择。Echo 是带有评测器的独立可选 OCI workload，不是 Catalyst 依赖。签名 Catalog 默认使用 `stable`；本预览版显式选择 `preview`。

The deployment profile is Native-first Hybrid: Platform and supervised Plugin
services are native, Catalyst uses its signed Python bundle, Echo uses OCI,
and the existing Web Client is served locally. Catalyst does not require
Docker. Product and Plugin removal preserves user datasets, artifacts, review
history, and backups by default.

部署采用 Native-first Hybrid：Platform 与受监督的 Plugin 服务以原生方式运行，Catalyst 使用签名 Python bundle，Echo 使用 OCI，现有 Web Client 由本机提供。Catalyst 不需要 Docker。默认卸载 Product 或 Plugin 时保留用户 Dataset、Artifact、审核历史和备份。

## Current official releases and source identities / 当前官方发行物与源码身份

| Evidence | Verified identity or state |
| --- | --- |
| Platform PR #103 / pending official P | Source and normal merge `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4` (PR #103 head `d5126f96059124be36af533e357438d388366099`); focused tests 61 passed (50 library, 11 binary); natural producer [run 38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) is building; release identity and strict asset verification **PENDING**. |
| Previously published Platform | Source P `64e4b58eb1dedc5ea328c07c9b07df55d2f38590`; [release](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-64e4b58eb1dedc5ea328c07c9b07df55d2f38590); run `38071761850`; API ID `409115567`; 52 assets, 24 manifests, 6 raw manifest subjects; strict no-skip verification passed. Bound by old Native W670. |
| Previously published Workspace / Native | Source W `670dcc640c7716484edacf5c0f77dd7ec93b5fea`; [release](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-670dcc640c7716484edacf5c0f77dd7ec93b5fea); run [`38083070781`](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/38083070781) attempt 1; API ID `409187818`; 18 assets; all four jobs succeeded; strict no-skip verification bound 21 source tuples and 8 exact source-verifier files. Immutable, but Host 2238 first-Core admission was blocked. |
| Ubuntu 24.04 amd64 DEB | [`Download`](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/native-installer-preview-670dcc640c7716484edacf5c0f77dd7ec93b5fea/cyrene_0.1.0-rc.1_ubuntu-24.04_amd64.deb); SHA-256 `4c0fc9f03054f8feb2154c893f5f3fd95ce71650340b099b83571c18bcad9deb`; 155,125,684 bytes |
| Native manifest / source receipt | Manifest SHA-256 `ca1c1d274427fd855d2424e943bc36950c185969e68bf5af9c9b0fc34703c89a`; source receipt SHA-256 `c83a5366c3a55d52386a22fe101ef744305f8459f26295aee12760a53b7fa8d5` |
| Active Catalog v2 | [`catalog-v2-preview-18455255cdf8b87dd8cd48ceb25e5ea411a622ad`](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/catalog-v2-preview-18455255cdf8b87dd8cd48ceb25e5ea411a622ad), generation 15, SHA-256 `9356861a377e18d1b6ed586d7ee8316de7d7d11c5e7543269d3cbd5d456f314d` |
| Host 2238 Stage70 | Catalyst staging **PASS**; first-Core Begin blocked with `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`; no runtime source activation; original guest/transaction preserved |
| Remaining acceptance | Catalyst install/apply, actual Plugin invocation, SFT export/load, Client/browser, Echo, reboot recovery, and full independent-host acceptance **PENDING / NOT_RUN** |

The Native source receipt and strict verification bind immutable W670 to previously published P64; this identity remains historical and must not be retargeted. Platform PR #103 is merged at `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4` (PR head `d5126f96059124be36af533e357438d388366099`). Its focused tests passed 61 tests (50 library, 11 binary). Natural official release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) is pending publication and strict verification; no new release ID or asset digest is claimed. A new Workspace SHA is required for a new immutable Native tag; this documentation-only source update preserves Catalog generation 15 and current Product/Plugin pins. Publication and identity checks do not substitute for host or product acceptance.

Native source receipt 与严格验签将不可变 W670 绑定到此前发布的 P64；该身份是历史发行身份，不得重定向。Platform PR #103 已于 `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4` 合并（PR head `d5126f96059124be36af533e357438d388366099`），定向测试共 61 项通过（library 50 项、binary 11 项）。正式 release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) 尚待发布和严格验证；本文不声明新的 release ID 或资产摘要。新不可变 Native tag 需要新的 Workspace SHA；本次文档 source 更新保留 Catalog generation 15 与现有 Product/Plugin pins。发行和身份核验不能代替主机或产品验收。

## Platform first-source contract / Platform 首次 source 契约

Platform [PR #103](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/103) merged as `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4` (head `d5126f96059124be36af533e357438d388366099`). Its focused tests passed 61 tests: 50 library and 11 binary tests. The source change is merged; natural official release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) has not yet produced a release verified by this guide. No release ID, asset digest, or attestation result is claimed.

The generic initial-source path has three guards:

1. The exact successful `BeginCoreBootstrap` parent may reserve `initial_source_artifact_ref = {source_id, component_id, artifact_digest}` from a verified signed Product identity, outside the six-member Core component digest map.
2. With that matching hold proof, the initial activity-source catalog may transition only from generation 0 with no registered sources or prior activity to generation 1 containing exactly the reserved source with zero binding scopes. Wrong owners, extra sources, bindings, or repeat initial writes are rejected.
3. `BeginInitialSourceActivation` must match the successful parent/source and a child `CORE_RUNTIME` plan carrying the same Product digest. Only a never-active source is eligible; ordinary readiness and active/stale-source checks remain fail-closed. `EndMaintenance` requires fresh authenticated source state and quiescent activity while the hold is active; missing or stale freshness does not unlock it.

The Platform path does not fabricate activity. The Product's authenticated `TasksReconciled` event establishes persisted freshness; periodic heartbeats continue through the normal SDK lifecycle. Catalog generation 15 and existing Product/Plugin pins remain unchanged. A new Native release needs a new Workspace SHA because the immutable tag is Workspace-source-specific; this documentation source update is intended to provide that identity after the Platform release is verified.

Platform [PR #103](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/103) 已于 `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4` 合并（PR head `d5126f96059124be36af533e357438d388366099`）。定向测试共 61 项通过：library 50 项、binary 11 项。源码已合并；正式 release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) 尚未产生经本文核验的 release，因此这里不声明 release ID、资产摘要或 attestation 结果。

通用首次 source 路径有三项守卫：

1. 精确成功的 `BeginCoreBootstrap` parent 可从已验证的签名 Product identity 保留 `initial_source_artifact_ref = {source_id, component_id, artifact_digest}`，并与六成员 Core component digest map 分开保存。
2. 持有匹配的 maintenance proof 时，初始 activity-source catalog 只能从 generation 0（无已注册 source、无既有 activity）转到 generation 1，且只含与预留 identity 完全一致、binding scope 为空的单个 source。错误 owner、多余 source、已有 binding 或重复初始写入都会被拒绝。
3. `BeginInitialSourceActivation` 必须匹配成功 parent/source，并要求 child `CORE_RUNTIME` plan 携带相同 Product digest。只有从未活动的 source 符合条件；普通 readiness 与已有/过期 source 检查保持 fail closed。`EndMaintenance` 在 hold 仍有效时要求真实认证 source 状态新鲜且 activity 静止；freshness 缺失或过期时不会解锁。

Platform 路径不会伪造 activity。Product 的认证 `TasksReconciled` event 建立持久化 freshness；周期 heartbeat 继续由正常 SDK 生命周期发送。Catalog generation 15 和现有 Product/Plugin pins 保持不变。由于不可变 Native tag 由 Workspace source SHA 确定，新 Native release 必须使用新的 Workspace SHA；本次文档 source 更新计划在 Platform release 通过验证后提供该身份。

## Stage70 initial-source lifecycle and acceptance gate / Stage70 首次 source 生命周期与验收门禁

The immutable W670/P64 pair is strictly verified as published, but its actual Host 2238 attempt is not a successful installation: Catalyst staging passed, then `BeginCoreBootstrap` returned `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`. The first-Core result remained pending/uncertain, runtime source activation did not begin, and the original guest and transaction state were preserved. The correction is in Platform PR #103, merged at `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4`; its focused tests passed 61 tests (50 library and 11 binary). Natural official release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) remains pending publication and strict verification at this checkpoint.

不可变 W670/P64 发行对的发布身份已严格验证，但 Host 2238 的实际尝试并未安装成功：Catalyst staging 通过后，`BeginCoreBootstrap` 返回 `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`。first-Core 结果仍是 pending/uncertain，runtime source activation 未开始，原 guest 与 transaction 状态均已保留。修正位于 Platform PR #103，已合并至 `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4`；定向测试共 61 项通过（library 50 项、binary 11 项）。截至本文记录时，正式 release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) 尚待发布和严格验证。

CLI command syntax retained from the previously published preview; these commands are not an acceptance run for the Stage70 source pair:

```bash
sudo cyrene workload install catalyst --channel preview --yes
sudo cyrene workload install echo --channel preview --yes
```

## Workspace integration and Stage70 acceptance boundary / Workspace 接线与 Stage70 验收边界

Workspace W670 recognizes the signed Catalyst `python-bundle` when deriving the initial source owner and keeps its exact Product artifact digest separate from the six-member C10 Core map. It records the generic initial-source context on the exact Core bootstrap parent. The Platform-side three-guard contract is described above; this ADR does not add a Catalyst-specific exception to Platform.

The corrected first-install sequence is: start the verified Product under the narrow initial-source hold; let its authenticated `TasksReconciled` reconciliation establish persisted source freshness; end the hold only after the Platform's exact parent/source and quiescence checks pass; then install and bind selected Plugins through the existing `PACKAGE_ONLY` flow. Workspace projects selected runtime refs into the separate root-owned `root:cyrene`, mode `0640` `catalyst-plugin-refs.env`, stores only digest/ownership metadata in the journal, and performs the regular Core-runtime restart so Catalyst reads the current refs. The packaged boot hook recovers dynamic refs through official Package Runtime `recover_binding` and `runtime_status` calls before Catalyst starts. Same-plan Begin/End replay, short-lived restart intent, protected-file backups, and exact journal identity constrain recovery; unknown configuration and unrelated lock conflicts fail closed.

The old W670 Native release remains immutable and bound to old P64. On Host 2238 its exact Stage70 Catalyst plan passed staging, then `BeginCoreBootstrap` was rejected with `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`. The first-Core result remained uncertain/pending; no Product start or runtime-source activation followed, and the original guest and transaction state were preserved. Platform PR #103 merged the generic contract at `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4`; its focused tests passed 61 tests (50 library and 11 binary). Natural release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) is still being built and has not yet yielded a release identity verified here. A new Native identity must use a new Workspace source SHA; Catalog generation 15 and the current Product/Plugin pins remain unchanged.

Workspace W670 在推导首次 source owner 时识别已签名的 Catalyst `python-bundle`，并将精确 Product artifact digest 与六成员 C10 Core map 分开；通用 initial-source context 记录在精确 Core bootstrap parent 上。Platform 侧三项守卫见上文；本 ADR 不向 Platform 添加 Catalyst 专用例外。

修正后的首次安装顺序为：在严格限定的 initial-source hold 中启动已验签 Product；由其认证 `TasksReconciled` reconciliation 建立持久化 source freshness；只有 Platform 精确核对 parent/source 且 activity 静止后才结束 hold；随后通过既有 `PACKAGE_ONLY` 流程安装和绑定所选 Plugin。Workspace 将所选 runtime ref 投影到独立的 root-owned `root:cyrene`、权限 `0640` 的 `catalyst-plugin-refs.env`，journal 仅记录 digest/ownership，并执行普通 Core-runtime restart 让 Catalyst 读取当前 refs。打包的 boot hook 在 Catalyst 启动前调用官方 Package Runtime `recover_binding` 与 `runtime_status` 恢复动态 refs。同 plan Begin/End replay、短时 restart intent、受保护文件备份和精确 journal identity 共同约束恢复；未知配置和无关锁冲突均 fail closed。

旧 W670 Native release 保持不可变并绑定旧 P64。Host 2238 的 Stage70 Catalyst 精确计划已通过 staging，随后 `BeginCoreBootstrap` 返回 `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`。first-Core 结果仍为 uncertain/pending；没有继续启动 Product 或 runtime source activation，原 guest 与 transaction 状态均已保留。Platform PR #103 已将通用契约合并至 `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4`；定向测试 61 项通过（library 50 项、binary 11 项）。正式 release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) 仍在构建，本文尚无经核验的 release identity。新的 Native identity 必须使用新的 Workspace source SHA；Catalog generation 15 和当前 Product/Plugin pins 保持不变。

## Customer installation and host prerequisites / 客户安装与主机前置条件

The reproducible Ubuntu 24.04 x86_64 protocol below documents the last published W670 Native preview and its verified release envelope. That immutable package is bound to old P64 and the Host 2238 first-Core Begin was blocked. Platform PR #103 is merged, but the new official P release is pending; a new Workspace commit is required for a new Native release identity. Do not use the old pair as an install target. Runtime, Plugin, SFT, Browser, Echo, reboot recovery, and independent-host acceptance remain **PENDING / NOT_RUN**.

Echo is independent and optional. It requires Docker Engine installed through Docker's official Ubuntu APT instructions, an active daemon, and the standard `/var/run/docker.sock`. Review package conflicts before changing them; avoid Docker's convenience script for production and do not grant broad access by adding users to the `docker` group. Verify using `sudo docker info` and `sudo docker run --rm hello-world`, then use `sudo cyrene workload install echo --channel preview --yes` only when the exact Echo host workflow has passed.

可复现的 Ubuntu 24.04 x86_64 流程说明如何取得并核验此前发布的 W670 Native preview 及其 release envelope。该不可变包绑定旧 P64，Host 2238 的 first-Core Begin 曾被阻止。Platform PR #103 修正源码已合并，但新的正式 P release 尚待完成；新 Native release 身份需要新的 Workspace commit。不要把旧发行对作为安装目标。runtime、Plugin、SFT、Browser、Echo、重启恢复和独立主机验收仍为 **PENDING / NOT_RUN**。

Echo 独立且可选。它要求通过 Docker 官方 Ubuntu APT 指南安装 Docker Engine、保持 daemon 运行，并提供标准 `/var/run/docker.sock`。修改软件包前检查冲突；不要在生产环境使用 Docker convenience script，也不要把用户加入 `docker` 组以授予宽泛权限。通过 `sudo docker info` 与 `sudo docker run --rm hello-world` 验证后，仅在确切 Echo 主机流程通过时再用 `sudo cyrene workload install echo --channel preview --yes`。

## Acceptance status and history / 验收状态与历史

The previously published P64 and W670 releases are immutable and strictly verified, but the Stage70 Host 2238 attempt stopped after successful staging: `BeginCoreBootstrap` was blocked with `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`. The first-Core result remained pending/uncertain; runtime source activation did not begin, and the original guest and transaction state were preserved. Platform PR #103 is merged, while its natural official release and strict asset verification remain pending. Catalyst runtime, Plugin invocation, boot-time binding recovery/restart, Browser, SFT, Echo, and independent-host acceptance are **PENDING / NOT_RUN**. Do not infer acceptance from release signatures, package installation, service health, or handoff readiness.

此前发布的 P64 与 W670 release 均不可变且通过严格验证，但 Stage70 Host 2238 尝试在成功 staging 后停止：`BeginCoreBootstrap` 因 `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED` 被阻止。first-Core 结果仍 pending/uncertain，runtime source activation 未开始，原 guest 与 transaction 状态已保留。Platform PR #103 已合并，其自然触发的正式 release 与严格资产验证仍待完成。Catalyst runtime、Plugin invoke、启动时 binding 恢复/重启、Browser、SFT、Echo 和独立主机验收仍为 **PENDING / NOT_RUN**。不得仅凭发行签名、包安装、服务健康或交接就绪推断验收通过。

### Previous W2 release / 较早的 W2 发行版本

The previous W2 Native release is immutable history, superseded by the current release, and does not verify acceptance for this source pair.

此前的 W2 Native release 是不可变历史发行物，现已被当前 release 取代，不能证明当前源码对已验收。

## Rollback and data retention / 回滚与数据保留

The public preview CLI documents workload installation and uninstall, not a user-selected downgrade to an arbitrary previous workload release. Platform's package runtime supports rollback for a managed maintenance transaction; this does not establish a general customer-facing version-selection command. On failure, retain the check/stage/apply plan, receipts, and transaction journal, stop at the reported blocker, and use the supported maintenance path. Do not manually edit Broker/runtime state or replace package files. Uninstalling Echo is documented to preserve Catalyst and user datasets, artifacts, review history, and backups by default. This lifecycle retention is not a backup guarantee; customers should keep independent backups before maintenance.

公开 preview CLI 文档提供 workload 安装与卸载，没有面向用户的任意历史版本降级命令。Platform package runtime 支持回滚受管理的 maintenance transaction；这不等于提供通用的客户版本选择命令。发生失败时保留 check/stage/apply plan、receipt 与 transaction journal，在报告的 blocker 处停止并使用受支持的 maintenance 流程。不要手工修改 Broker/runtime 状态或替换包文件。文档中的 Echo 卸载默认保留 Catalyst、用户 Dataset、Artifact、审核历史和备份。生命周期保留不构成备份保证；维护前仍应保留独立备份。

## Operational contract / 运行契约

- Install from exact immutable, signed releases. Verify the full release envelope, asset hashes, source bindings, and detached attestations before installation. Never use `latest`, replace an asset, or bypass the trusted package lifecycle.
- Required components cannot be excluded. Recommended components are selected by default and may be explicitly excluded; a missing capability must remain visible. Each of the four Catalyst data plugins can also be installed alone through the `plugins` workload.
- Keep Client and Control listeners on loopback. Use an approved SSH local forward for remote Client access.
- `check`, `stage`, and `apply` use the same plan ID, digest, and channel. Review the resolved rows before apply; stop on a blocker and preserve receipts.
- Echo is a separate optional workload. Removing Echo must leave Catalyst and user datasets, artifacts, review history, and backups intact.
- Keep the task branch/worktree locked while external release or host-acceptance evidence is pending. After completion, use the guarded cleanup helper with `--only-branch <task-branch>`; never unlock another owner's worktree or use broad recursive deletion.

- 只从确切不可变的签名发行物安装。安装前验证完整发行包、资产摘要、source binding 和 detached attestation。不得使用 `latest`、替换发行资产或绕过可信包生命周期。
- 必需组件不能取消；推荐组件默认选择且可显式排除，缺失 capability 必须明确显示。四个 Catalyst 数据插件也可通过 `plugins` workload 单独安装。
- Client 与 Control listener 保持 loopback 绑定。远程访问使用获准的 SSH 本地转发。
- `check`、`stage`、`apply` 使用相同 plan ID、digest 和 channel。apply 前检查解析后的组件；遇到 blocker 即停止并保留 receipt。
- Echo 是独立可选 workload。卸载 Echo 后，Catalyst、用户 Dataset、Artifact、审核历史和备份必须保留。
- 等待外部发行物或主机验收证据期间，任务 worktree 应保持锁定。完成后通过带 `--only-branch <task-branch>` 的受保护清理 helper 收尾；不得解锁其他 owner 的 worktree，也不得广泛递归删除。
