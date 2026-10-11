# ADR: Modular distribution preview / 模块化发行预览

## Status / 状态

The v0.1 architecture is accepted. Workspace W `b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3` (PR #152 merge) and Platform P `99aa35d75397ab01746d90d6ca078fe20dda0c1e` (PR #107 merge) now have immutable source-bound releases. The Native release passed official release API and asset-byte checks, and its exact-source verifier passed all nine detached asset attestations without skip or trust cache. Plugins Official G `48f9148e987c6e9066038db99ae0295542e32a77` and signed Catalog generation 15 from `18455255cdf8b87dd8cd48ceb25e5ea411a622ad` remain unchanged. Root's combined source, canonical pins, closure, and Ubuntu 24.04 package-payload readback also passed. Fresh-host product acceptance remains PENDING.

v0.1 架构已接受。Workspace W `b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3`（PR #152 merge）和 Platform P `99aa35d75397ab01746d90d6ca078fe20dda0c1e`（PR #107 merge）现均有源码绑定的不可变 release。Native release 已通过官方 release API 与资产字节核对；精确源码 verifier 对九个 detached asset attestation 全部严格验证通过，未跳过、未使用信任缓存。Plugins Official G `48f9148e987c6e9066038db99ae0295542e32a77` 与源码 `18455255cdf8b87dd8cd48ceb25e5ea411a622ad` 发布的已签名 Catalog generation 15 保持不变。Root 综合源码、canonical pins、closure 与 Ubuntu 24.04 包 payload 回读也已通过。全新主机产品验收仍待完成。

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

The subsequent retired P86/W a3 package-only apply on guest2246 failed with `MAINTENANCE_ADMISSION_DENIED` and retained an active Standard PackageOnly hold. Platform PR #107 adds a durable `SourceHeartbeat` for the empty reconciliation case; Workspace PR #151 fixes pre-Begin generation reuse and same-held-plan replay. These changes target the source paths but do not migrate or clear guest2246's existing transaction. The public workload protocol exposes no supported cancellation or recovery operation for that active hold. Preserve the VM disk, journal, and hold; do not issue direct Broker RPCs, clear state, or start a second plan. Guest2246 is not claimed recovered; fresh-host acceptance is tracked separately.

A separate attempt with the superseded W0d48 Native release on a fresh test host reached Dataset Generation but failed with `OFFLINE_CANDIDATE_IDENTITY_MISMATCH`: the offline adapter compared the outer release-manifest digest with the inner Plugin manifest digest. Workspace PR #152 fixes that binding.

The replacement b9068f0 Native release passed official publication, asset, source, and attestation checks, and its DEB was installed and read back on a test host. The one authorized same-plan apply then failed for `cyrene-tools-dataset-generation` with `PACKAGE_INSTALL_FAILED` / `DEPENDENCY_PREPARE_FAILED`; the original hold and journal were retained. The current diagnosis is an incomplete offline dependency closure: the Dataset Generation 0.2.1 package archive has no wheelhouse although its lock requires `grpcio==1.62.3` and `protobuf==4.25.9`, while Platform runs with `UV_OFFLINE=1` and the Native wheelhouse lacks these distributions. The official Plugin package and Workspace catalog/lock closure are being corrected before a new immutable Native release is published. Do not retry this held plan; final acceptance will use a clean host. SFT generation, Client browser access, Echo evaluation, and reboot recovery remain NOT_RUN.

后续已退役的 P86/W a3 package-only apply 在 guest2246 上以 `MAINTENANCE_ADMISSION_DENIED` 失败，并保留 active Standard PackageOnly hold。Platform PR #107 为空 reconciliation 情况增加持久化 `SourceHeartbeat`；Workspace PR #151 修复 Begin 前 generation 重用与同一 hold 下的 plan replay。这些变更针对源码路径，但不会迁移或清除 guest2246 已有事务。公开 workload protocol 没有该 active hold 受支持的取消或恢复操作。保留 VM 磁盘、journal 和 hold；不要直接调用 Broker RPC、清除状态或启动第二个 plan。不能声称 guest2246 已恢复；全新主机验收单独记录。

此前在全新测试主机上使用已被替代的 W0d48 Native release 运行到 Dataset Generation 时，以 `OFFLINE_CANDIDATE_IDENTITY_MISMATCH` 失败：offline adapter 将外层 release-manifest digest 与 Plugin 内层 manifest digest 比较。Workspace PR #152 修正了该绑定。

替代版本 b9068f0 Native release 已通过官方发布、资产、源码和 attestation 检查；其 DEB 也已在测试主机安装并完成回读。但唯一获准的同一 plan apply 在 `cyrene-tools-dataset-generation` 失败，错误为 `PACKAGE_INSTALL_FAILED` / `DEPENDENCY_PREPARE_FAILED`；原 hold 与 journal 已保留。当前诊断为离线依赖闭包不完整：Dataset Generation 0.2.1 包归档没有 wheelhouse，但 lock 要求 `grpcio==1.62.3` 和 `protobuf==4.25.9`；Platform 设置 `UV_OFFLINE=1`，Native wheelhouse 也缺少这两个发行包。团队正在补齐官方 Plugin 包及 Workspace catalog/lock 闭包，然后发布新的不可变 Native release。不要重试这个已有 hold 的 plan；最终验收将在干净主机进行。SFT 生成、Client 浏览器访问、Echo 评测和重启恢复仍为 NOT_RUN。

Published releases remain immutable. P86 and the W a3 Native release are historical Stage78 evidence only. The current official Platform release is `preview-99aa35d75397ab01746d90d6ca078fe20dda0c1e` (API 409660341; producer run 38137876503); all 52 assets matched the release API, with 24 indexed manifests and 25 strict verification receipts. The current Native release is `native-installer-preview-b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3` (API 409705856; producer run 38144970974), built from exact W `b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3` source (PR #152). All 18 assets matched API IDs/digests/sizes, and the exact-source strict verifier passed nine detached attestations without skip or trust cache. The Ubuntu 24.04 DEB is SHA-256 `d240c03a8635ebe1e78a18330400c035b898ab62e56baf9abd1a6c4985b0b6b4`, 155111334 bytes (asset 630904057). Root's full-release readback SHA-256 is `60781f5bce08d2b28b82fb230bff04d46b830afcec232c7ff910bc7fc0e43780`; canonical pins SHA-256 `e614d27101fa818ca06f4cdb302be338783e68c789853e4804f71e35d3e4db7a`; pins/closure readback SHA-256 `09ebf1e7c335bfe49ffa84abbccfc4149153933bdc345a0ad7435225646686cf`; Ubuntu 24.04 payload readback SHA-256 `92f0d50351ea8aa70ab0f1027cc33c596d1f8a8fc3ab507dfce2bb2275327c58`. The older W2327, W a3/P86, and superseded W0d48 Native releases are not valid for this acceptance.

已发布的 release 保持不可变。P86 与 W a3 Native release 仅作为 Stage78 历史证据。当前 Platform 官方 release 为 `preview-99aa35d75397ab01746d90d6ca078fe20dda0c1e`（API 409660341；producer run 38137876503）；52 项资产均与 release API 匹配，24 个 index manifest 与 25 份严格验证记录通过。当前 Native release 为 `native-installer-preview-b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3`（API 409705856；producer run 38144970974），基于精确 W `b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3` 源码（PR #152）构建。18 项资产均与 API 的 ID/摘要/大小匹配，精确源码严格 verifier 对九个 detached attestation 全部通过，未跳过、未使用信任缓存。Ubuntu 24.04 DEB 的 SHA-256 为 `d240c03a8635ebe1e78a18330400c035b898ab62e56baf9abd1a6c4985b0b6b4`，大小 155111334 字节（asset 630904057）。Root full-release 回读 SHA-256 `60781f5bce08d2b28b82fb230bff04d46b830afcec232c7ff910bc7fc0e43780`；canonical pins SHA-256 `e614d27101fa818ca06f4cdb302be338783e68c789853e4804f71e35d3e4db7a`；pins/closure 回读 SHA-256 `09ebf1e7c335bfe49ffa84abbccfc4149153933bdc345a0ad7435225646686cf`；Ubuntu 24.04 payload 回读 SHA-256 `92f0d50351ea8aa70ab0f1027cc33c596d1f8a8fc3ab507dfce2bb2275327c58`。旧 W2327、W a3/P86 与已被替代的 W0d48 Native release 不适用于本次验收。

## Release identities / 发行身份

| Repository / artifact | Exact source or version | Published identity / status |
| --- | --- | --- |
| Workspace / Native | `b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3` (PR #152 merge) | [Immutable release](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3), API 409705856; producer run 38144970974; all 18 assets matched; nine strict detached attestations passed without skip or trust cache |
| Ubuntu 24.04 amd64 DEB | API asset 630904057 | SHA-256 `d240c03a8635ebe1e78a18330400c035b898ab62e56baf9abd1a6c4985b0b6b4`; 155,111,334 bytes |
| Platform | `99aa35d75397ab01746d90d6ca078fe20dda0c1e` (PR #107 merge) | [Immutable release](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-99aa35d75397ab01746d90d6ca078fe20dda0c1e), API 409660341; run 38137876503; 52 assets matched; 24 indexed manifests and 25 strict verification receipts passed |
| Historical Platform P86 / Native W a3 | P86 source `86ee31721113d548d0dccfa88bc7e080df9cd1f9`; Native W `a3bcc1a3b34c6417dc538887896de7a5358f3d6d` | Immutable release and strict asset verification passed; not the current release candidate and not evidence that guest2246's active hold was recovered |
| Catalyst | `4ad95061bde8c5d216a8cb7b30e1ef2ae1b4d49b` | `cyrene-catalyst` bundle version `cdbc66391cb43eee2c66248f1dfd07f2cf2cd09db4e079374bd24415f4b4b1ba`; release `preview-4ad95061bde8c5d216a8cb7b30e1ef2ae1b4d49b` |
| Plugins Official | `48f9148e987c6e9066038db99ae0295542e32a77` | Five immutable source-bound packages; producer run 38123576893; 80 official assets and 40 strict attestation subjects verified |
| Echo | `d5a920078077bdbaa76e7117ad83f69cbfa1c614` | `cyrene-echo` 0.1.0; `ghcr.io/dohorizon-ai/cyrene-echo@sha256:aa4c70c15e9907c81874333fae0cb64b0444d9ab3d4a27c6f2dee7a20c7b5d4c` |
| Client | `b3c3f964540e3aa761612371891e3bb6a3f7ad59` | Workspace Control and Web `0.0.1+sha.b3c3f964540e3aa761612371891e3bb6a3f7ad59` |
| Active v2 Catalog | source `18455255cdf8b87dd8cd48ceb25e5ea411a622ad`, generation 15 | `catalog-v2-preview-18455255cdf8b87dd8cd48ceb25e5ea411a622ad`; SHA-256 `9356861a377e18d1b6ed586d7ee8316de7d7d11c5e7543269d3cbd5d456f314d`; unchanged |

| 仓库 / 制品 | 精确源码或版本 | 已发布身份 / 状态 |
| --- | --- | --- |
| Workspace / Native | `b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3`（PR #152 merge） | [不可变 release](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3)，API 409705856；producer run 38144970974；18 项资产匹配；9 个 detached attestation 严格验证通过，未跳过、未使用信任缓存 |
| Ubuntu 24.04 amd64 DEB | API asset 630904057 | SHA-256 `d240c03a8635ebe1e78a18330400c035b898ab62e56baf9abd1a6c4985b0b6b4`；155,111,334 字节 |
| Platform | `99aa35d75397ab01746d90d6ca078fe20dda0c1e`（PR #107 merge） | [不可变 release](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-99aa35d75397ab01746d90d6ca078fe20dda0c1e)，API 409660341；run 38137876503；52 项资产匹配；24 个 index manifest 与 25 份严格验证记录通过 |
| 历史 Platform P86 / Native W a3 | P86 源码 `86ee31721113d548d0dccfa88bc7e080df9cd1f9`；Native W `a3bcc1a3b34c6417dc538887896de7a5358f3d6d` | 不可变 release 与资产严格验证已通过；不是当前 release 候选，也不代表 guest2246 的 active hold 已恢复 |
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
| Historical Platform P86 release API identity and assets | PASS: 52 assets matched the immutable API release |
| Historical Platform P86 strict source-bound attestations | PASS: 24 indexed manifests; 26 verifier processes; 31 attested subjects; no skip or trust cache |
| Current Platform P99 official release and strict verification | PASS: API release 409660341; 52 API-matched assets; 24 indexed manifests; 25 strict verification receipts |
| Five Plugins G48 package identities and strict attestations | PASS: 5 immutable releases; 80 assets; 40 subjects; no skip or trust cache |
| Historical Native W a3 publication and complete verification | PASS: immutable API release 409611808; run 38133372833; 18 API-matched assets; nine detached asset attestations; strict verification without skip or trust cache |
| Current Native publication and strict verification | PASS: API release 409705856; producer run 38144970974; 18 API-matched assets; nine detached attestations passed without skip or trust cache |
| Ubuntu 24.04 current Native source and package payload readback | PASS: exact W source and DEB SHA/size verified by the Root release reader |
| Consolidated current-release source, pins, and DEB payload readback | PASS: Native full-release readback `60781f5bce08d2b28b82fb230bff04d46b830afcec232c7ff910bc7fc0e43780`; canonical pins `e614d27101fa818ca06f4cdb302be338783e68c789853e4804f71e35d3e4db7a`; pins/closure `09ebf1e7c335bfe49ffa84abbccfc4149153933bdc345a0ad7435225646686cf`; Ubuntu 24.04 payload `92f0d50351ea8aa70ab0f1027cc33c596d1f8a8fc3ab507dfce2bb2275327c58` |
| Clean-host installation bound to the current Native release | PENDING final release-bound record |
| Catalyst workload resolution, installation, Plugin invocation, and data workflow | PENDING |
| Client browser access and SFT package consumer | PENDING |
| Restart recovery and Echo install/evaluation/removal | PENDING |
| Full clean-host acceptance bound to current release identities | PENDING final release-bound record |
| Guest2246 old P86 PackageOnly hold recovery | NOT SUPPORTED / NOT CLAIMED: disk, journal, and hold retained; no cancel/recovery operation is exposed by the public workload protocol |
| Historical W2327 first Catalyst stage | BLOCKED on the retired release; not evidence for current Native acceptance |

| 检查 | 状态 |
| --- | --- |
| 历史 Platform P86 release API 身份与资产 | 通过：52 项资产与不可变 API release 匹配 |
| 历史 Platform P86 源码绑定 attestation 严格验证 | 通过：24 个 index manifest；26 个 verifier 进程；31 个 attested subject；未跳过、未使用信任缓存 |
| 当前 Platform P99 官方 release 与严格验证 | 通过：API release 409660341；52 项资产与 API 匹配；24 个 index manifest；25 份严格验证记录 |
| 五个 Plugins G48 包身份与严格 attestation | 通过：5 个不可变 release；80 项资产；40 个 subject；未跳过、未使用信任缓存 |
| 历史 Native W a3 发布与完整验证 | 通过：不可变 API release 409611808；run 38133372833；18 项资产与 API 匹配；9 个 detached asset attestation；严格验证未跳过、未使用信任缓存 |
| 当前 Native release 发布与严格验证 | 通过：API release 409705856；producer run 38144970974；18 项资产与 API 匹配；9 个 detached attestation 严格验证通过，未跳过、未使用信任缓存 |
| Ubuntu 24.04 当前 Native 源码与包 payload 回读 | 通过：Root release reader 核对精确 W 源码及 DEB 摘要/大小 |
| 当前 release 的综合源码、pins 与 DEB payload 回读 | 通过：Native full-release 回读 `60781f5bce08d2b28b82fb230bff04d46b830afcec232c7ff910bc7fc0e43780`；canonical pins `e614d27101fa818ca06f4cdb302be338783e68c789853e4804f71e35d3e4db7a`；pins/closure `09ebf1e7c335bfe49ffa84abbccfc4149153933bdc345a0ad7435225646686cf`；Ubuntu 24.04 payload `92f0d50351ea8aa70ab0f1027cc33c596d1f8a8fc3ab507dfce2bb2275327c58` |
| 绑定当前 Native release 的全新主机安装 | 等待绑定 release 身份的最终记录 |
| Catalyst workload 解析、安装、Plugin 调用与数据流程 | 待完成 |
| Client 浏览器访问与 SFT 包 consumer | 待完成 |
| 重启恢复与 Echo 安装/评测/移除 | 待完成 |
| 绑定当前 release 身份的全新主机端到端验收 | 等待绑定 release 身份的最终记录 |
| guest2246 旧 P86 PackageOnly hold 恢复 | 不支持 / 未声称完成：保留磁盘、journal 和 hold；公开 workload protocol 未提供 cancel/recovery 操作 |
| 历史 W2327 首次 Catalyst stage | 旧 release 上受阻；不作为当前 Native 验收证据 |

## Operations / 运维约定

Install only exact immutable releases after verifying checksums, source bindings, and detached attestations. Use one plan identity for check, stage, and apply. Stop on blockers and keep receipts and the transaction journal; do not edit Broker state or replace package files manually.

只在校验 checksum、源码绑定和 detached attestation 后安装确切不可变发行物。check、stage、apply 使用同一 plan identity。遇到 blocker 即停止，并保留 receipt 与 transaction journal；不要编辑 Broker 状态或手动替换包文件。

Uninstall preserves user datasets, artifacts, review history, and backups by default, but this is not a backup guarantee. This preview has no customer-facing arbitrary-version downgrade command.

默认卸载会保留用户 Dataset、Artifact、审核历史和备份，但不构成备份保证。此预览版没有面向客户的任意版本降级命令。

See the [first-user installation guide](MODULAR_DISTRIBUTION_V01.md).

首次安装步骤见[用户安装指南](MODULAR_DISTRIBUTION_V01.md)。
