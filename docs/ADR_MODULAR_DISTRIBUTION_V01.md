# ADR: Modular distribution preview / 模块化发行预览

## Status / 状态

The current Ubuntu 24.04 x86_64 Native preview is Workspace W `03453ed1c053151c9af86a2c0a185747b55c3968` (PR #166), published as [native-installer-preview-03453ed1c053151c9af86a2c0a185747b55c3968](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-03453ed1c053151c9af86a2c0a185747b55c3968), release API `410782500`, producer run `38272522755`. The immutable release has 18/18 downloaded assets matching API SHA-256 digests and byte sizes; the exact-source verifier passed 19 attestation invocations (nine direct and ten nested), with skip and trust cache disabled. Its Ubuntu 24.04 DEB SHA-256 is `12987b876a6918608efa6973a9029d0840517d6e4a52e5252cb4ef1f0e4271b6` (`155115228` bytes). Platform P `587d35588e8590abab5bd645ba09bbc480479daa`, Plugins G `c9b8d4b7d4923d42368de3b3ec2db87ee97c3fa7` (SDK `0.2.1`), signed Catalog generation 15, and selected Product identities are reused unchanged from Stage93. PR #166 fixes C10 authority-source selection from the validated signed plan while retaining authority checks. Stage93 guest2253 recorded `PLAN_CHANGED` and `PACKAGE_RUNTIME_READBACK_REQUIRED` failures with causes not established, then `FIRST_CORE_READBACK_REQUIRED` from the confirmed C10 source-selection bug. A later recovery Apply reported overall success with the SDK and six pointers healthy; those earlier failures remain recorded. Stage94 same-guest upgrade and public Apply, along with final core acceptance, remain pending.

当前 Ubuntu 24.04 x86_64 Native preview 使用 Workspace W `03453ed1c053151c9af86a2c0a185747b55c3968`（PR #166），已发布为 [native-installer-preview-03453ed1c053151c9af86a2c0a185747b55c3968](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-03453ed1c053151c9af86a2c0a185747b55c3968)，release API `410782500`，producer run `38272522755`。不可变发行物的 18 项下载资产均与 API SHA-256 和字节大小匹配；精确源码 verifier 的 19 次 attestation 调用（9 个直接、10 个嵌套）全部通过，未跳过或使用信任缓存。Ubuntu 24.04 DEB SHA-256 为 `12987b876a6918608efa6973a9029d0840517d6e4a52e5252cb4ef1f0e4271b6`（`155115228` 字节）。Platform P `587d35588e8590abab5bd645ba09bbc480479daa`、带 SDK `0.2.1` 的 Plugins G `c9b8d4b7d4923d42368de3b3ec2db87ee97c3fa7`、已签名 Catalog generation 15 和所选 Product 身份沿用 Stage93 的固定值。PR #166 修复了从已验证签名 plan 选择 C10 authority source 的问题，同时保留 authority 校验。Stage93 guest2253 记录了 `PLAN_CHANGED` 和 `PACKAGE_RUNTIME_READBACK_REQUIRED` 失败，原因未能确认；随后 `FIRST_CORE_READBACK_REQUIRED` 的原因确认为 C10 source 选择 bug。之后一次 recovery Apply 报告 overall success，SDK 与六个 pointers 健康；此前失败仍保留。Stage94 同一主机 upgrade、public Apply 和最终 core 验收仍待完成。

The Catalyst/Echo cloud update check remains unverified because its pinned catalog helper rejects `deploymentRoles` in `compatibilityGroups.0` before Azure deployment. This is a cloud helper/schema limitation; it does not affect the local Ubuntu Native release path. The earlier Stage91 `PACKAGE_RUNTIME_READBACK_REQUIRED` Apply failure is historical and is not evidence for or against Stage94. Stage94 runtime and workload acceptance remain pending its actual receipts.

Catalyst/Echo 云端更新检查仍未验证，因为固定版本 catalog helper 在 Azure 部署前拒绝了 `compatibilityGroups.0` 中的 `deploymentRoles`。这是云端 helper/schema 限制，不影响本机 Ubuntu Native 发行流程。此前 Stage91 的 `PACKAGE_RUNTIME_READBACK_REQUIRED` Apply 失败属于历史记录，不能作为 Stage94 的通过或失败证据。Stage94 runtime 和 workload 验收仍待回读实际回执。

For the current user installation sequence, start with the [Ubuntu installation guide](MODULAR_DISTRIBUTION_V01.md#stage94-native-install).

当前用户安装步骤请从[Ubuntu 安装指南](MODULAR_DISTRIBUTION_V01.md#stage94-native-install)开始。

## Decision / 决策

Workspace owns workload selection, signed Catalog resolution, release discovery, and check/stage/apply plans. Platform supplies generic package, maintenance, Broker, and activation mechanisms; product composition remains outside Platform Core.

The `preview` channel resolves the trusted candidate available when a plan is created. That plan pins immutable release identities and digests, so later preview updates do not change it. Record the actual installed source tuple from plan and installed-state readbacks.

Workspace 负责 workload 选择、签名 Catalog 解析、发行物发现以及 check/stage/apply plan。Platform 提供通用包管理、maintenance、Broker 和 activation 机制；产品组合留在 Platform Core 之外。

`preview` channel 会在创建 plan 时解析当时受信的候选版本。该 plan 固定不可变 release 身份和 digest，因此后续 preview 更新不会改变它。应从 plan 和 installed-state 回读记录实际安装的来源 tuple。

Catalyst is a Native workload. Required components are Catalyst service, Client Control, Client Web, and Runtime Maintenance SDK. Dataset Preparation, Document Parsing, Dataset Generation, and Knowledge Preparation are recommended Plugins selected by default. Each can also be installed alone through the `plugins` workload. Missing optional capabilities must be visible.

Catalyst 是 Native workload。必需组件为 Catalyst service、Client Control、Client Web 和 Runtime Maintenance SDK。Dataset Preparation、Document Parsing、Dataset Generation、Knowledge Preparation 是默认选中的推荐 Plugin，也可以通过 `plugins` workload 单独安装。缺少可选 capability 时必须明确显示。

Echo is an independent optional OCI workload with an evaluator, not a Catalyst dependency. Echo requires Docker Engine; Catalyst and Plugins do not.

Echo 是带评测器的独立可选 OCI workload，不是 Catalyst 依赖。Echo 需要 Docker Engine；Catalyst 和 Plugin 不需要。

Deployment is Native-first Hybrid: Platform and supervised Plugins run natively, Catalyst uses a signed Python bundle, Echo uses OCI, and the existing Web Client is served locally. Client and Control stay on loopback; remote access uses an approved SSH local forward.

部署采用 Native-first Hybrid：Platform 与受监督 Plugin 以原生方式运行，Catalyst 使用签名 Python bundle，Echo 使用 OCI，现有 Web Client 由本机提供。Client 与 Control 仅监听 loopback；远程访问使用获准的 SSH 本地转发。

## Runtime contracts and acceptance boundary / Runtime 契约与验收边界

The implementation keeps package identity, manifest digests, detached signatures, source bindings, and plan confirmation strict. Workspace owns workload composition and plan recovery; Platform owns generic package/runtime and maintenance behavior. Runtime checks must compare the exact selected release and installed state, preserve package-provided dependency closure, and leave rollback/recovery evidence available. Fixes were merged through normal Workspace and Platform PRs; historical guest failures are not treated as proof for the current release.

实现继续严格校验 package 身份、manifest digest、detached signature、源码绑定和 plan 确认。Workspace 负责 workload 组合和 plan 恢复；Platform 负责通用 package/runtime 与 maintenance 行为。Runtime 检查必须对比精确选定的 release 和 installed state，保留 package 提供的依赖闭包，并使 rollback/recovery 证据可读。相关修复通过正常 Workspace 和 Platform PR 合并；历史 guest 失败不能作为当前 release 的验收结果。

The public retry path is the original saved plan through `sudo cyrene workload --json` with `operation:"apply"`; guided `workload install --yes` does not expose the plan identity on failure, and status does not list pending plans. Preserve the displayed `planId`, `planDigest`, and channel. The preview has no customer-facing arbitrary-version downgrade command.

公开恢复入口是通过 `sudo cyrene workload --json` 对保存的原 plan 执行 `operation:"apply"`；guided `workload install --yes` 失败时不会显示 plan 身份，status 也不列出 pending plan。请保存界面显示的 `planId`、`planDigest` 和 channel。当前 preview 没有面向客户的任意版本降级命令。

Stage94 official publication, API/asset readback, exact-source readback, and strict provenance verification passed. Stage93’s three guest2253 Apply failures remain distinct: the causes of `PLAN_CHANGED` and `PACKAGE_RUNTIME_READBACK_REQUIRED` are not established; `FIRST_CORE_READBACK_REQUIRED` was caused by the C10 probe falling back to `cyrene-yield` instead of using the validated signed Catalyst source. A later recovery Apply reported overall success with the SDK and six pointers healthy. Stage94 same-guest upgrade, new-source public Apply, and full core/Echo/recovery/reboot acceptance remain pending. Do not infer runtime acceptance from publisher signatures, package installation, or CI.

Stage94 官方发布、API/资产回读、精确源码回读和严格来源验证均已通过。Stage93 guest2253 的三次 Apply 失败分别保留：`PLAN_CHANGED` 和 `PACKAGE_RUNTIME_READBACK_REQUIRED` 的原因未能确认；`FIRST_CORE_READBACK_REQUIRED` 的原因是 C10 probe 回退到 `cyrene-yield`，没有使用已验证签名的 Catalyst source。之后一次 recovery Apply 报告 overall success，SDK 与六个 pointers 健康。Stage94 同一主机 upgrade、新源码 public Apply，以及完整 core/Echo/recovery/reboot 验收仍待执行。不能从 publisher 签名、package 安装或 CI 推断 runtime 验收通过。

## Operations / 运维约定

Install only exact immutable releases after verifying checksums, source bindings, and detached attestations. Use one plan identity for check, stage, and apply. Stop on blockers and keep receipts and the transaction journal; do not edit Broker state or replace package files manually.

只在校验 checksum、源码绑定和 detached attestation 后安装确切不可变发行物。check、stage、apply 使用同一 plan identity。遇到 blocker 即停止，并保留 receipt 与 transaction journal；不要编辑 Broker 状态或手动替换包文件。

Uninstall preserves user datasets, artifacts, review history, and backups by default, but this is not a backup guarantee. This preview has no customer-facing arbitrary-version downgrade command.

默认卸载会保留用户 Dataset、Artifact、审核历史和备份，但不构成备份保证。此预览版没有面向客户的任意版本降级命令。

See the [first-user installation guide](MODULAR_DISTRIBUTION_V01.md).

首次安装步骤见[用户安装指南](MODULAR_DISTRIBUTION_V01.md)。
