# ADR: Modular distribution preview / 模块化发行预览

Status: The immutable Ubuntu 24.04 amd64 Native preview from Workspace source
`0e8d8f56cc3bb827320a85671fff43220d0a4551` is published and independently
verified. The producer workflow completed all four jobs. Strict verification
passed for 18 assets, 21 bound tuples, seven verifier source blobs, and 20
release evidence records with no skipped checks; the independent root
readback also passed. The 34-gate fresh-host run on Ubuntu 24.04.5 guest 2234
ended with 15 PASS, 1 FAIL, and 18 NOT_RUN. `catalyst_workload_apply` failed
with retryable `FIRST_CORE_BOOTSTRAP_FAILED` when the first-Core Kernel service
rejected its sandbox peer UID/GID arguments. Catalyst runtime activation,
Client access, SFT production and independent loading, and Echo lifecycle have
not passed acceptance. The Platform source correction is merged in
[Cyrene-Platform PR #100](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/100)
at `2bbd40b3b30cd9401127a033dd33cf6902378536`, and required CI passed. The
official Platform release [run 38039153155](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38039153155),
attempt 1, was still in progress at the 2026-10-10 09:09 UTC checkpoint.
Catalog verification and both Ubuntu native-build jobs had succeeded, and
`component-release` was running `Run release test gates`. No release for P was
published and strict release verification remained pending. This is an interim checkpoint,
not final host-acceptance evidence; a verified Platform release and a new
immutable Native preview must pass the remaining gates before this candidate
can be accepted for a customer trial.

状态：Workspace 源码 `0e8d8f56cc3bb827320a85671fff43220d0a4551` 对应的不可变 Ubuntu 24.04 amd64 Native 预览版已发布并通过独立验证。Producer workflow 的四个 job 全部成功。严格验证通过 18 项资产、21 个绑定 tuple、7 个 verifier 源文件和 20 条发行证据，未跳过检查；独立 root 回读也通过。Ubuntu 24.04.5 主机 2234 的 34 项全新主机验收以 15 项通过、1 项失败、18 项未运行结束。`catalyst_workload_apply` 因 first-Core Kernel 服务拒绝 sandbox peer UID/GID 参数而以可重试的 `FIRST_CORE_BOOTSTRAP_FAILED` 失败。Platform 源码修正已合并于 PR #100（`2bbd40b3b30cd9401127a033dd33cf6902378536`），required CI 已通过。2026-10-10 09:09 UTC checkpoint 回读时，正式 Platform 发行流程 [run 38039153155](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38039153155) 第 1 次尝试仍在运行：Catalog 验证与 Ubuntu 22.04/24.04 原生构建均已成功，`component-release` job 正在执行 `Run release test gates`。尚未发布 P 的 release，严格发行验证仍待完成。本文是阶段 checkpoint，不是最终主机验收证据；正式 Platform 发行验证和新的不可变 Native 预览版通过剩余验收后，才能接受该候选版本用于客户试用。Catalyst runtime 激活、Client 访问、SFT 生成与独立加载、Echo lifecycle 均未通过验收。

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

## Verified preview identity / 已验证的预览发行身份

Workspace source `0e8d8f56cc3bb827320a85671fff43220d0a4551` (PR #135 merge)
published the immutable prerelease
[`native-installer-preview-0e8d8f56cc3bb827320a85671fff43220d0a4551`](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-0e8d8f56cc3bb827320a85671fff43220d0a4551).
Producer run
[`38034438591`](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/38034438591)
attempt 2 succeeded in all four jobs; the release API ID is `408779397`.

Workspace 源码 `0e8d8f56cc3bb827320a85671fff43220d0a4551`（PR #135 merge）发布了不可变预览版
[`native-installer-preview-0e8d8f56cc3bb827320a85671fff43220d0a4551`](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-0e8d8f56cc3bb827320a85671fff43220d0a4551)。Producer run
[`38034438591`](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/38034438591)
attempt 2 的四个 job 全部成功；release API ID 为 `408779397`。

| Evidence / 证据 | Identity / 身份 |
| --- | --- |
| Ubuntu 24.04 amd64 DEB | `cyrene_0.1.0-rc.1_ubuntu-24.04_amd64.deb`; SHA-256 `ddb6548d824fd6cdc40ca3635e43e60a648bd43100661f4f5e0d26124d7b2280`; 155,106,734 bytes |
| Release manifest | `native-installer-release-v2.json`; SHA-256 `85a0153696140d54ebb9c5c552ffc7434fa92414c942ed7b5a56c59768e481ca` |
| Native source receipt | `native-installer-source-receipt-v2.json`; SHA-256 `ba5d91023be13429f57c298baf7c92cad88b3399b4d2bd3f4b66bc9bd531a8ce` |
| Active Catalog v2 | `catalog-v2-preview-18455255cdf8b87dd8cd48ceb25e5ea411a622ad`, generation 15, source `18455255cdf8b87dd8cd48ceb25e5ea411a622ad`; Catalog SHA-256 `9356861a377e18d1b6ed586d7ee8316de7d7d11c5e7543269d3cbd5d456f314d`; attestation bundle SHA-256 `37762cbdabe99c5e62fa9494531ff7e16962b241d2cff47bf920530a33585e85` |
| Frozen component identities | Platform `preview-daabf9ff561b4ab9296094d8198fd1fa3418b32b`; Catalyst `preview-4ad95061bde8c5d216a8cb7b30e1ef2ae1b4d49b`; Plugins Official source `443da5e071c9d7e82e7f28788cc04777e0bea1b9`; Echo source `d5a920078077bdbaa76e7117ad83f69cbfa1c614`; Client Web `preview-cyrene-client-workspace-web-b3c3f964540e3aa761612371891e3bb6a3f7ad59` |
| Independent verification | Strict release verification and independent root asset/source readback passed; root full readback SHA-256 `a8e0396cc98cea109f0c7fd736ac41b2dc2586468afb6e02dde8ede0762a3f0f` |

已发布发行物的确切 DEB、manifest、source receipt 与 active Catalog 身份如上。严格发行验证通过全部 18 项资产、21 个绑定 tuple、7 个 verifier 源文件和 20 条 evidence，未跳过检查；独立 root 资产/源码回读也通过。上述发行证据只确认发行物身份与完整性，不代表主机安装、Catalyst/Core 启动、Client 访问、SFT 消费者加载或 Echo lifecycle 已通过。

The installation protocol and component-selection commands are in the
[Ubuntu installation guide](MODULAR_DISTRIBUTION_V01.md). The pinned 0e
candidate's package identity is verified, but its fresh-host Catalyst apply
failed, so it is not an accepted customer-trial release. Keep the preview
channel explicit and verify the release envelope and detached attestations
before any installation. Do not use `latest`, manually copy Catalog data, or
set `PYTHONPATH`.

安装协议与组件选择命令见 [Ubuntu 安装指南](MODULAR_DISTRIBUTION_V01.md)。已固定的 0e 候选版本身份通过了发行验证，但全新主机 Catalyst apply 失败，因此它尚未成为通过客户试用验收的发行版。Platform 源码修正已在 PR #100 的 SHA `2bbd40b3b30cd9401127a033dd33cf6902378536` 合并，required CI 已通过；正式 Platform preview 发行和严格验证仍待完成。任何安装前均须显式选择 preview 通道，并验证完整发行包与 detached attestation。不要使用 `latest`、手工复制 Catalog 数据或设置 `PYTHONPATH`。

## Acceptance result and correction path / 验收结果与修正路径

The terminal ledger for the fresh Ubuntu 24.04.5 guest 2234 contains 34 known
gates: 15 PASS, 1 FAIL, 18 NOT_RUN, and no unknown gates. The only failed gate
is `catalyst_workload_apply`; it returned retryable
`FIRST_CORE_BOOTSTRAP_FAILED`. The first-Core transaction entered
`hold_required` at `cohort_starting` because `cyrene-kernel.service` exited
with status 1.

Read-only diagnosis against the frozen Platform source
`daabf9ff561b4ab9296094d8198fd1fa3418b32b` confirmed an argument-contract
mismatch: the signed unit and managed runtime pass sandbox peer UID/GID as
named values (`sandboxd=0` and `sandboxd=992`), while the Kernel parser expects
unsigned integer scalars. The correction is merged in
[Cyrene-Platform PR #100](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/100)
at `2bbd40b3b30cd9401127a033dd33cf6902378536`; required CI passed. It changes
the shipped Kernel unit and managed-runtime sandbox peer UID/GID arguments to
unsigned scalar values and adds regression coverage for parsing the shipped
unit arguments and rejecting adapter-named values. The official Platform
release [run 38039153155](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38039153155),
attempt 1, was still in progress at the 2026-10-10 09:09 UTC checkpoint.
Catalog verification and both Ubuntu native-build jobs had succeeded, and
`component-release` was running `Run release test gates`. No release for P was
published and strict release verification remained pending, so this source commit was not yet
an installable verified component. Record the exact Platform release and
verification receipt before preparing the follow-on Native preview.

The published 0e Native release is immutable and its tag is derived from the
Workspace source SHA. Do not overwrite or retarget it. This interim
documentation PR establishes the next Workspace source, W; it does not change
the source Catalog or dependency lock. After the corrected Platform release
P passes strict verification, dispatch the existing Native producer from W
with `release_inputs_json.platformReleaseId=preview-P`. The producer will
create the next immutable Native preview using the existing
`native-installer-preview-<Workspace-SHA>` tag convention. Keep the existing
`0.1.0-rc.1` package version, active-v2 Catalog generation 15, and all other
frozen Product, component, and profile identities; this checkpoint does not
introduce a Catalog, tag-scheme, or version change. This checkpoint does not
claim a successful install:
Core runtime, Catalyst product activation, Client access, SFT producer and
independent consumer, restart/reboot recovery, plugin supervision, and Echo
lifecycle remain NOT_RUN. Report those results only after the matching gates
pass on the follow-on immutable preview. Preserve the failed guest and its
receipts until the owner completes the follow-up acceptance; do not edit
Broker state by hand or infer runtime success from package signatures.

全新 Ubuntu 24.04.5 主机 2234 的终态 ledger 共包含 34 个已知 gate：15 项 PASS、1 项 FAIL、18 项 NOT_RUN，且没有未知 gate。唯一失败项是 `catalyst_workload_apply`，返回可重试的 `FIRST_CORE_BOOTSTRAP_FAILED`。first-Core transaction 在 `cohort_starting` 阶段进入 `hold_required`，原因是 `cyrene-kernel.service` 以状态码 1 退出。

针对冻结的 Platform 源码 `daabf9ff561b4ab9296094d8198fd1fa3418b32b` 进行的只读诊断确认了参数契约不匹配：signed unit 和 managed runtime 以命名形式传递 sandbox peer UID/GID（`sandboxd=0` 与 `sandboxd=992`），而 Kernel parser 要求 unsigned integer scalar。修正已在 [Cyrene-Platform PR #100](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/100) 合并至 `2bbd40b3b30cd9401127a033dd33cf6902378536`，required CI 已通过。该修改把 shipped Kernel unit 和 managed runtime 的 sandbox peer UID/GID 参数改为 unsigned scalar，并增加 shipped unit 参数解析和拒绝 adapter-named 参数的回归覆盖。2026-10-10 09:09 UTC checkpoint 回读时，正式 Platform 发行流程 [run 38039153155](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38039153155) 第 1 次尝试仍在运行：Catalog 验证与 Ubuntu 22.04/24.04 原生构建均已成功，`component-release` job 正在执行 `Run release test gates`。尚未发布 P 的 release，严格发行验证仍待完成，因此该源码提交还不是可安装且已验证的组件。准备后续 Native 预览版前，必须记录确切 Platform release 和验证 receipt。

已发布的 0e Native release 不可变，且 tag 由 Workspace source SHA 派生。不得覆盖或重定向。本次阶段文档 PR 合并后会建立新的 Workspace source W；它不会修改 source Catalog 或 dependency lock。Platform 修正版 P 通过严格验证后，再从 W 调用现有 Native producer，并在 `release_inputs_json.platformReleaseId` 中设置 `preview-P`。Producer 沿用现有 `native-installer-preview-<Workspace-SHA>` tag 规则创建新的不可变 Native 预览版。保留 `0.1.0-rc.1` 包版本、active-v2 Catalog generation 15 及其他已冻结的 Product、component、profile identity；本 checkpoint 不引入新的 Catalog、tag scheme 或版本变更。本 checkpoint 不宣称安装成功：Core runtime、Catalyst product 激活、Client 访问、SFT producer 与独立 consumer、restart/reboot 恢复、plugin supervision 和 Echo lifecycle 均为 NOT_RUN。只有后续不可变预览版上的对应 gate 通过后，才能报告这些结果。owner 完成后续验收前，保留失败主机及其 receipts；不要手工修改 Broker 状态，也不要根据包签名推断 runtime 成功。

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
