# ADR: Modular distribution preview / 模块化发行预览

## Status / 状态

The v0.1 architecture is accepted. The active source identities are Workspace W `2327cf9221a23721b64017f326b5ba47cf2bc3fd`, Platform P `86ee31721113d548d0dccfa88bc7e080df9cd1f9`, and Plugins Official G `48f9148e987c6e9066038db99ae0295542e32a77`. Their current immutable releases are published and strictly verified. Clean-host acceptance is in progress and remains PENDING.

v0.1 架构已接受。当前源码身份为 Workspace W `2327cf9221a23721b64017f326b5ba47cf2bc3fd`、Platform P `86ee31721113d548d0dccfa88bc7e080df9cd1f9`、Plugins Official G `48f9148e987c6e9066038db99ae0295542e32a77`。各仓库当前对应的不可变 release 均已发布并通过严格验证。全新主机验收正在进行，状态仍为待完成。

## Decision / 决策

Workspace owns workload selection, signed Catalog resolution, release discovery, and check/stage/apply plans. Platform supplies generic package, maintenance, Broker, and activation mechanisms; product composition remains outside Platform Core.

Workspace 负责 workload 选择、签名 Catalog 解析、发行物发现以及 check/stage/apply plan。Platform 提供通用包管理、maintenance、Broker 和 activation 机制；产品组合留在 Platform Core 之外。

Catalyst is a Native workload. Required components are Catalyst service, Client Control, Client Web, and Runtime Maintenance SDK. Dataset Preparation, Document Parsing, Dataset Generation, and Knowledge Preparation are recommended Plugins selected by default. Each can also be installed alone through the `plugins` workload. Missing optional capabilities must be visible.

Catalyst 是 Native workload。必需组件为 Catalyst service、Client Control、Client Web 和 Runtime Maintenance SDK。Dataset Preparation、Document Parsing、Dataset Generation、Knowledge Preparation 是默认选中的推荐 Plugin，也可以通过 `plugins` workload 单独安装。缺少可选 capability 时必须明确显示。

Echo is an independent optional OCI workload with an evaluator, not a Catalyst dependency. Echo requires Docker Engine; Catalyst and Plugins do not.

Echo 是带评测器的独立可选 OCI workload，不是 Catalyst 依赖。Echo 需要 Docker Engine；Catalyst 和 Plugin 不需要。

Deployment is Native-first Hybrid: Platform and supervised Plugins run natively, Catalyst uses a signed Python bundle, Echo uses OCI, and the existing Web Client is served locally. Client and Control stay on loopback; remote access uses an approved SSH local forward.

部署采用 Native-first Hybrid：Platform 与受监督 Plugin 以原生方式运行，Catalyst 使用签名 Python bundle，Echo 使用 OCI，现有 Web Client 由本机提供。Client 与 Control 仅监听 loopback；远程访问使用获准的 SSH 本地转发。

## Package Runtime corrections / Package Runtime 修正

Two distinct failures were observed during real package-only apply attempts. First, `/run/cyrene-package-runtime` was absent while the daemon was stopped. Platform PR #105 added `RuntimeDirectoryPreserve=yes` and an isolated systemd regression. A later attempt reached `/run/cyrene-package-runtime/control.sock`, created for `cyrene:cyrene` (UID 999, GID 988) with mode `0660`, but bootstrap's generic `mode & 0022` check rejected it. Platform PR #106 permits only this exact socket path when it has the expected socket type, runtime UID/GID, and `0660` mode. The existing mode rejection remains for other checked paths and objects, and a live/listening socket is still rejected. No guest-side `chmod` was used.

真实 package-only apply 发现了两个独立问题。首先，daemon 停止时 `/run/cyrene-package-runtime` 目录不存在。Platform PR #105 增加 `RuntimeDirectoryPreserve=yes` 和隔离 systemd 回归。后续尝试到达 `/run/cyrene-package-runtime/control.sock`；该 socket 属于 `cyrene:cyrene`（UID 999、GID 988），权限为 `0660`，但 bootstrap 通用 `mode & 0022` 检查将其拒绝。Platform PR #106 仅允许这个确切 socket 路径在类型、runtime UID/GID 和 `0660` 权限均符合预期时通过。其他受检查路径和对象仍保留原权限拒绝规则，活动/监听 socket 仍会被拒绝。修复未使用 guest 侧 `chmod`。

Published releases remain immutable. Platform PR #106 is published as `preview-86ee31721113d548d0dccfa88bc7e080df9cd1f9`. The current Native is the separate immutable release `native-installer-preview-2327cf9221a23721b64017f326b5ba47cf2bc3fd`, built from exact Workspace source W2327. The failed historical pairs must not be reused.

已发布的 release 保持不可变。Platform PR #106 对应 release 为 `preview-86ee31721113d548d0dccfa88bc7e080df9cd1f9`。当前 Native 是从精确 Workspace 源码 W2327 构建的独立不可变 release `native-installer-preview-2327cf9221a23721b64017f326b5ba47cf2bc3fd`。不得复用失败的历史发行对。

## Release identities / 发行身份

| Repository / artifact | Exact source or version | Published identity / status |
| --- | --- | --- |
| Workspace / Native | `2327cf9221a23721b64017f326b5ba47cf2bc3fd` | [Immutable Native release](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-2327cf9221a23721b64017f326b5ba47cf2bc3fd), API 409550498; producer run 38125742126 attempt 1; 18 assets match API IDs/digests/sizes; strict no-skip/no-trust-cache verification passed with nine detached asset attestations |
| Ubuntu 24.04 amd64 DEB | API asset 630267360 | SHA-256 `d9da6f3cd9c72f55e6d27e7ff4219f5acdb77c2699504ef3df153393b628b1c9`; 155,116,908 bytes |
| Platform | `86ee31721113d548d0dccfa88bc7e080df9cd1f9` | [P86 release](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-86ee31721113d548d0dccfa88bc7e080df9cd1f9), API 409542106, producer run 38123069265; immutable; 52/52 API asset identities match; 24 indexed manifests and 31 attested subjects strictly verified |
| Catalyst | `4ad95061bde8c5d216a8cb7b30e1ef2ae1b4d49b` | `cyrene-catalyst` bundle version `cdbc66391cb43eee2c66248f1dfd07f2cf2cd09db4e079374bd24415f4b4b1ba`; release `preview-4ad95061bde8c5d216a8cb7b30e1ef2ae1b4d49b` |
| Plugins Official | `48f9148e987c6e9066038db99ae0295542e32a77` | Five immutable source-bound packages; producer run 38123576893; 80 official assets and 40 strict attestation subjects verified |
| Echo | `d5a920078077bdbaa76e7117ad83f69cbfa1c614` | `cyrene-echo` 0.1.0; `ghcr.io/dohorizon-ai/cyrene-echo@sha256:aa4c70c15e9907c81874333fae0cb64b0444d9ab3d4a27c6f2dee7a20c7b5d4c` |
| Client | `b3c3f964540e3aa761612371891e3bb6a3f7ad59` | Workspace Control and Web `0.0.1+sha.b3c3f964540e3aa761612371891e3bb6a3f7ad59` |
| Active v2 Catalog | source `18455255cdf8b87dd8cd48ceb25e5ea411a622ad`, generation 15 | `catalog-v2-preview-18455255cdf8b87dd8cd48ceb25e5ea411a622ad`; SHA-256 `9356861a377e18d1b6ed586d7ee8316de7d7d11c5e7543269d3cbd5d456f314d`; unchanged |

| 仓库 / 制品 | 精确源码或版本 | 已发布身份 / 状态 |
| --- | --- | --- |
| Workspace / Native | `2327cf9221a23721b64017f326b5ba47cf2bc3fd` | [不可变 Native release](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-2327cf9221a23721b64017f326b5ba47cf2bc3fd)，API 409550498；producer run 38125742126、attempt 1；18 项资产与 API 的 ID/摘要/大小匹配；9 个 detached asset attestation 严格验证通过，未跳过、未使用信任缓存 |
| Ubuntu 24.04 amd64 DEB | API asset 630267360 | SHA-256 `d9da6f3cd9c72f55e6d27e7ff4219f5acdb77c2699504ef3df153393b628b1c9`；155,116,908 字节 |
| Platform | `86ee31721113d548d0dccfa88bc7e080df9cd1f9` | [P86 release](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-86ee31721113d548d0dccfa88bc7e080df9cd1f9)，API 409542106、producer run 38123069265；不可变；52/52 API 资产身份匹配；24 个 index manifest 与 31 个 attested subject 严格验证通过 |
| Catalyst | `4ad95061bde8c5d216a8cb7b30e1ef2ae1b4d49b` | `cyrene-catalyst` bundle 版本 `cdbc66391cb43eee2c66248f1dfd07f2cf2cd09db4e079374bd24415f4b4b1ba`；release `preview-4ad95061bde8c5d216a8cb7b30e1ef2ae1b4d49b` |
| Plugins Official | `48f9148e987c6e9066038db99ae0295542e32a77` | 五个源码绑定的不可变包；producer run 38123576893；80 项官方资产与 40 个严格 attestation subject 已验证 |
| Echo | `d5a920078077bdbaa76e7117ad83f69cbfa1c614` | `cyrene-echo` 0.1.0；`ghcr.io/dohorizon-ai/cyrene-echo@sha256:aa4c70c15e9907c81874333fae0cb64b0444d9ab3d4a27c6f2dee7a20c7b5d4c` |
| Client | `b3c3f964540e3aa761612371891e3bb6a3f7ad59` | Workspace Control 与 Web `0.0.1+sha.b3c3f964540e3aa761612371891e3bb6a3f7ad59` |
| 当前 v2 Catalog | 源码 `18455255cdf8b87dd8cd48ceb25e5ea411a622ad`，generation 15 | `catalog-v2-preview-18455255cdf8b87dd8cd48ceb25e5ea411a622ad`；SHA-256 `9356861a377e18d1b6ed586d7ee8316de7d7d11c5e7543269d3cbd5d456f314d`；保持不变 |

Each G48 package has its own 16-asset immutable release, source-bound to the exact Plugins commit:

| Plugin package | Version | Release API ID |
| --- | --- | --- |
| Dataset Preparation | 0.2.0 | [409524198](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-tools-dataset-preparation-0.2.0-48f9148e987c6e9066038db99ae0295542e32a77) |
| Document Parsing | 0.2.0 | [409524177](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-tools-document-parsing-0.2.0-48f9148e987c6e9066038db99ae0295542e32a77) |
| Dataset Generation | 0.2.1 | [409524170](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-tools-dataset-generation-0.2.1-48f9148e987c6e9066038db99ae0295542e32a77) |
| Knowledge Preparation | 0.1.0 | [409524121](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-tools-knowledge-preparation-0.1.0-48f9148e987c6e9066038db99ae0295542e32a77) |
| Exact Match | 0.1.0 | [409524157](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-evaluation-exact-match-0.1.0-48f9148e987c6e9066038db99ae0295542e32a77) |

The Native source receipt and release manifest bind the exact component locks for this release; those signed artifacts are authoritative if a summary table differs.

G48 每个 Plugin 包都有独立的 16 项源码绑定不可变 release：

| Plugin 包 | 版本 | Release API ID |
| --- | --- | --- |
| Dataset Preparation | 0.2.0 | [409524198](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-tools-dataset-preparation-0.2.0-48f9148e987c6e9066038db99ae0295542e32a77) |
| Document Parsing | 0.2.0 | [409524177](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-tools-document-parsing-0.2.0-48f9148e987c6e9066038db99ae0295542e32a77) |
| Dataset Generation | 0.2.1 | [409524170](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-tools-dataset-generation-0.2.1-48f9148e987c6e9066038db99ae0295542e32a77) |
| Knowledge Preparation | 0.1.0 | [409524121](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-tools-knowledge-preparation-0.1.0-48f9148e987c6e9066038db99ae0295542e32a77) |
| Exact Match | 0.1.0 | [409524157](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-evaluation-exact-match-0.1.0-48f9148e987c6e9066038db99ae0295542e32a77) |

Native source receipt 与 release manifest 绑定此发行版的精确组件锁定；若摘要表有差异，以这些已签名制品为准。

## Acceptance record / 验收记录

| Check | Status |
| --- | --- |
| Platform P86 official release API identity and assets | PASS |
| Platform P86 strict source-bound attestations | PASS: 24 indexed manifests; 26 verifier processes; 31 attested subjects; no skip or trust cache |
| Five Plugins G48 package identities and strict attestations | PASS: 5 immutable releases; 80 assets; 40 subjects; no skip or trust cache |
| Native official publication and complete verification | PASS: immutable API release 409550498; 18 API-matched assets; nine detached asset attestations; strict verification without skip or trust cache |
| Ubuntu 24.04 host initialization, preflight, DEB installation and static readback | PASS |
| Catalyst first stage | PENDING: retryable Plugin re-resolution NETWORK_ERROR; original plan retained; stage/apply not completed |
| Plugin invocation, Client access, SFT export/consumer, restart recovery, Echo lifecycle | PENDING |
| Full clean-host acceptance | PENDING |

| 检查 | 状态 |
| --- | --- |
| Platform P86 官方 release API 身份与资产 | 通过 |
| Platform P86 源码绑定 attestation 严格验证 | 通过：24 个 index manifest；26 个 verifier 进程；31 个 attested subject；未跳过、未使用信任缓存 |
| 五个 Plugins G48 包身份与严格 attestation | 通过：5 个不可变 release；80 项资产；40 个 subject；未跳过、未使用信任缓存 |
| Native 官方发布与完整验证 | 通过：不可变 API release 409550498；18 项资产与 API 匹配；9 个 detached asset attestation；严格验证未跳过、未使用信任缓存 |
| Ubuntu 24.04 主机初始化、preflight、DEB 安装和静态回读 | 通过 |
| Catalyst 首次 stage | 待完成：Plugin 重新解析返回可重试 NETWORK_ERROR；原计划已保留；stage/apply 尚未完成 |
| Plugin 调用、Client 访问、SFT 导出/consumer、重启恢复、Echo 生命周期 | 待完成 |
| 全新主机端到端验收 | 待完成 |

## Operations / 运维约定

Install only exact immutable releases after verifying checksums, source bindings, and detached attestations. Use one plan identity for check, stage, and apply. Stop on blockers and keep receipts and the transaction journal; do not edit Broker state or replace package files manually.

只在校验 checksum、源码绑定和 detached attestation 后安装确切不可变发行物。check、stage、apply 使用同一 plan identity。遇到 blocker 即停止，并保留 receipt 与 transaction journal；不要编辑 Broker 状态或手动替换包文件。

Uninstall preserves user datasets, artifacts, review history, and backups by default, but this is not a backup guarantee. This preview has no customer-facing arbitrary-version downgrade command.

默认卸载会保留用户 Dataset、Artifact、审核历史和备份，但不构成备份保证。此预览版没有面向客户的任意版本降级命令。

See the [first-user installation guide](MODULAR_DISTRIBUTION_V01.md).

首次安装步骤见[用户安装指南](MODULAR_DISTRIBUTION_V01.md)。
