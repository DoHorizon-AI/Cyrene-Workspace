# ADR: Modular distribution preview / 模块化发行预览

Status: The immutable Ubuntu 24.04 amd64 Native preview from Workspace source
`0e8d8f56cc3bb827320a85671fff43220d0a4551` is published and independently
verified. The producer workflow completed all four jobs. Strict verification
passed for 18 assets, 21 bound tuples, seven verifier source blobs, and 20
release evidence records with no skipped checks; the independent root
readback also passed. Fresh-host acceptance was handed off to Ubuntu 24.04
guest 2234, but a complete authoritative phase readback is not yet recorded.
No Catalyst/Core, Client, SFT-consumer, or Echo acceptance is claimed.

状态：Workspace 源码 `0e8d8f56cc3bb827320a85671fff43220d0a4551` 对应的不可变 Ubuntu 24.04 amd64 Native 预览版已发布并通过独立验证。Producer workflow 的四个 job 全部成功。严格验证通过 18 项资产、21 个绑定 tuple、7 个 verifier 源文件和 20 条发行证据，未跳过检查；独立 root 回读也通过。Ubuntu 24.04 主机 2234 已收到全新主机验收交接，但尚未记录完整且权威的阶段回读。本 ADR 不宣称 Catalyst/Core、Client、SFT consumer 或 Echo 验收通过。

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

The reproducible install and component-selection commands are in the
[Ubuntu installation guide](MODULAR_DISTRIBUTION_V01.md). Install Catalyst with
`sudo cyrene workload install catalyst --channel preview --yes`. Keep the
preview channel explicit and verify the release envelope and detached
attestations before installation. Do not use `latest`, manually copy Catalog
data, or set `PYTHONPATH`.

可复现的安装与组件选择命令见 [Ubuntu 安装指南](MODULAR_DISTRIBUTION_V01.md)。使用
`sudo cyrene workload install catalyst --channel preview --yes` 安装 Catalyst。始终显式选择 preview 通道，并在安装前验证完整发行包和 detached attestation。不要使用 `latest`、手工复制 Catalog 数据或设置 `PYTHONPATH`。

## Acceptance boundary / 验收边界

The formal fresh-host handoff authorizes acceptance on guest 2234. The final
host phase record is still pending a complete authoritative readback. Report
only results backed by the corresponding frozen receipt; package signature
verification is not a substitute for runtime acceptance. Keep Catalyst/Core,
Client, independent SFT-consumer, and Echo lifecycle results unclaimed until
those phase receipts are available. Preserve the plan and receipts and do not
edit Broker state by hand.

正式全新主机交接已授权在 guest 2234 上开展验收。主机各阶段的最终记录仍待完整且权威的回读。只报告对应冻结 receipt 支持的结果；发行包签名验证不能代替运行时验收。在取得相应阶段 receipt 前，不宣称 Catalyst/Core、Client、独立 SFT consumer 或 Echo lifecycle 通过。保留 plan 和 receipts，不要手工修改 Broker 状态。

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
