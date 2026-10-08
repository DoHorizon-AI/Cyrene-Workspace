# ADR: Modular distribution preview / 模块化发行预览

Status: accepted for the Ubuntu 24.04 preview architecture. Matching modular DEB, Client Control, and official Linux Echo releases and clean-host acceptance remain separate pending gates.

状态：Ubuntu 24.04 预览配置采用本决策；正式发布与安装后验收必须分别提供实际证据。

## Context / 背景

Document Parsing, Dataset Preparation, Dataset Generation, Knowledge Preparation,
and the exact-match evaluator already have source and capability contracts. Their
absence from independently installable, signed packages is a distribution gap.
Reimplementing these capabilities or a second package registry would not resolve
that gap. Catalyst already has an official native Python bundle, and Echo already
has an OCI publisher. Earlier installer research must be checked against current
repository refs, release assets, and exact image attestations.

上述五个插件已有源码及能力契约，当前缺口是独立打包、发布、发现和激活。Catalyst
已有原生 Python bundle，Echo 已有 OCI 发布链；旧调研的结论必须用当前代码、正式
资产和精确 image digest 重新核实，不能作为重新实现能力或注册表的依据。

## Authority and selection / 权威与选择

- Workspace owns the versioned component catalog, workload selection, dependency
  resolution, install preview, deployment receipts, and maintenance orchestration.
- Platform owns package installation, source authorization, binding lifecycle,
  supervision, runtime locks, and authenticated maintenance admission.
- Plugins own their existing implementations, Package Spec, exact dependency lock,
  and independently signed package releases.
- Products own business operations and their data. Client consumes the shared
  workload-plan API and publishes its existing Web workbench.

Workspace 管理目录、选择和维护装配；Platform 管理正式包生命周期和能力授权；
Plugins 管理具体能力及独立发布；Product 管理业务和数据；Client 使用同一安装计划
契约。安装流程不增加平行的 Registry、Runtime 或安装数据库。

Catalyst requires its native Product bundle and the Web Client. Its four data
plugins are recommended by default, so a user may deselect a parser and still
start the Product. Missing functionality must report a structured capability
error. Echo is a separate optional workload with its evaluator recommended by
default. A standalone plugin selection does not pull in either Product or the
Client. The signed catalog declares required support components, including the
Package Runtime, maintenance services, and the operator SDK needed for actual
binding operations.

Catalyst 必需自己的原生 bundle 和 Web Client，默认推荐四个数据插件；取消某插件后
Product 仍可启动，相应功能返回明确的缺失能力错误。Echo 是独立可选工作负载，默认
推荐评测插件。独立安装插件不安装 Product 或 Client；运行时、维护服务和 operator
SDK 等支持依赖由受信目录明确声明。

Every source ID and per-owner binding ID is explicit catalog data. Runtime UID,
GID, token digest, and installed identities come from protected local receipts and
actual runtime facts. Component IDs and package IDs are different namespaces;
neither one may be inferred by changing punctuation in the other. The plan digest
binds the action, selection, exact release identities, source mappings, and bindings.

source ID、按 owner 隔离的 binding ID 必须在目录中显式声明；UID/GID、凭据摘要及
安装身份从受保护的本机事实读取。component ID 与 package ID 属于不同命名空间，
不能靠替换标点互相推断。计划摘要绑定操作、选择、发行身份及来源和 binding 映射。

## Deployment / 部署

The initial profile is Native-first Hybrid:

| Component | Ubuntu 24.04 deployment |
| --- | --- |
| Platform and maintenance | Existing native components and systemd services |
| Plugins | Independent packages supervised by Platform |
| Catalyst | Official native Python bundle with its locked environment |
| Client | Signed static Web archive served through Workspace deployment configuration |
| Echo | Optional official linux/amd64 OCI image |

首发采用上述原生优先混合方案。Catalyst 的 OCI 路径继续保留，不替换当前原生入口。

The Workspace implementation candidate serves the activated Client static-Web
root through a managed Nginx listener on `127.0.0.1:8100` and proxies same-origin
Studio API routes to local Client Control on `127.0.0.1:5182`. The Control and
Web listeners remain loopback-only; remote access uses an existing approved SSH
forward. This host configuration does not expose the installer through the
Platform Workspace Web BFF and does not, by itself, prove that release assets
or a clean-host installation are accepted. See
[`MODULAR_DISTRIBUTION_V01.md`](MODULAR_DISTRIBUTION_V01.md) for the operator
entrypoint and pending release pins.

Workspace 实现候选通过受管 Nginx 在 `127.0.0.1:8100` 提供已激活的 Client 静态 Web 根目录，并将同源
Studio API 路由转发到本机 Client Control `127.0.0.1:5182`。Control 与 Web listener 仅绑定 loopback；
远程访问使用已有且获准的 SSH 转发。此主机配置不会经由 Platform Workspace Web BFF 暴露安装器，也不能单独证明
release assets 或全新主机安装已验收。操作入口与待填发行 pin 见
[`MODULAR_DISTRIBUTION_V01.md`](MODULAR_DISTRIBUTION_V01.md)。

Platform's current plugin readiness contract returns a host-loopback gRPC
connection reference. Its package-control and maintenance Unix sockets are
control-plane sockets; they are not plugin invocation sockets. Native Catalyst
can use that reference directly. For optional Echo on Linux, use host networking,
the image's non-root identity, a read-only root filesystem, dropped capabilities,
and a Product listener bound to `127.0.0.1`. Persist its data through the approved
data mounts. Do not publish a plugin listener to `0.0.0.0` or use a privileged
container. This preview uses the existing trusted-local-host transport boundary;
it does not claim cryptographic authentication for the plugin's loopback gRPC
data plane. Control and maintenance operations retain their peer identity,
source-token and Broker admission checks.

当前 plugin connection_ref 是宿主回环 gRPC 地址；包控制与维护 UDS 不是插件调用
通道。原生 Catalyst 直接使用该地址；Linux Echo 采用 host networking、镜像非 root
身份、只读根文件系统、关闭额外 capabilities，Product 仅监听 `127.0.0.1`，数据保存在
批准的挂载中。预览版沿用受信本机边界，不宣称回环 gRPC 数据面具有额外密码学认证；
控制与维护操作继续校验 peer 身份、来源 token 和 Broker admission。

A real transport smoke with the existing immutable Echo digest
`sha256:3aa667dc3c8f7e2403ca6db55a4074443703fcf9b2fe6854911ae91485c9bb91`
completed exact-match evaluation and read persisted results after container
recreation. That endpoint was started by the Plugin owner implementation, not
Platform. It establishes transport feasibility, not installer activation. The
release acceptance must separately invoke the Platform-supervised endpoint.

现有精确 Echo digest 的实际容器测试完成了评测和重建后的数据回读，但插件端点由
owner 实现直接启动。因此它只证明传输方案可行；正式安装验收必须调用 Platform
监督的插件端点，不能将该 smoke 当成安装激活通过。

## Release compatibility / 发布兼容

Catalog v1 remains strict and retains `catalog-<channel>-<source SHA>` releases.
Catalog v2 uses `catalog-v2-<channel>-<source SHA>` and the exact v2 catalog and
attestation asset pair. New consumers prefer v2 and may fall back to v1. Old
pinned v1 publishers continue to discover v1 releases. Both versions keep exact
workflow, source ref, source SHA, immutable-release and detached-attestation
checks. Published assets are never replaced or re-signed as another version.

保留旧 v1 strict 契约及发行前缀；v2 使用独立版本前缀和精确的两个资产。新版优先
发现 v2、缺失时回退 v1；旧版固定 verifier 继续发现 v1。两代发行都保留身份、不可变
资产和 attestation 检查，旧资产不被覆盖或重新签名冒充新版本。

The package ZIP digest is distinct from the Package Spec's member digest.
Workspace verifies the published bytes and exact descriptor; Platform validates
the descriptor, dependency lock and package members through its formal lifecycle.
Static Web content is also a typed component, rather than an executable Product
bundle. Publisher identities distinguish multiple official workflows in one
repository.

包 ZIP 摘要与 Package Spec 成员摘要不同；Workspace 验证正式发行字节，Platform
通过正式生命周期验证 descriptor、lock 和成员。静态 Web 使用专门的组件类型。
同一仓库的多个官方发布 workflow 具有可区分的 publisher identity。

## Recovery and acceptance / 恢复与验收

Check, stage and apply share `cyrene.workload-plan.v1`. Install and uninstall
actions are bound into the same plan and confirmation chain. Binding mutations
first persist their owner outcome and complete real Broker admission. Offline
package changes require the exact `PACKAGE_ONLY` hold and shared Runtime lock;
activation starts after that hold ends. Removing a component used by another
owner must preserve it or produce an explicit blocker. Product removal retains
datasets, artifacts, review history and backups by default.

安装和卸载共用同一 plan/check/stage/apply 确认链。binding 操作先持久化 owner 结果并
完成 Broker admission；离线包变更要求精确 PACKAGE_ONLY hold 和运行时共享锁；
激活在 hold 结束后执行。仍被其他 owner 使用的组件保留或明确阻止卸载。默认保留
用户 Dataset、Artifact、审核历史和备份。

The release gate is a fresh Ubuntu 24.04 host consuming official release assets
without cloning source: Catalyst selection, verified component installation,
Platform activation, Product startup, Web access, real review and SFT export,
and independent exported-package verification. Optional Echo must then complete
real evaluation and independent removal while Catalyst and user data remain.
Repeat installation, interruption/retry, missing components, disconnection,
version conflict, failed upgrade and rollback have individual evidence statuses.
A container-only base probe, a staged DEB, local green tests or transport smoke
cannot be reported as this gate passing.

At the time of this ADR update, the matching generation-15 native DEB, Client
Control release, and official Linux Echo release are still pending; full clean-host
acceptance is `NOT_RUN`. Treat the installation commands as instructions for the
exact future immutable release pins, not as evidence that the release or host
workflow has passed.

最终门禁是在全新 Ubuntu 24.04 上仅消费官方发布物，完成 Catalyst 安装、激活、Web
访问、真实审核和 SFT 导出及独立读取，再验证可选 Echo 的评测和独立删除。可靠性
场景逐项记录 PASS/FAIL/NOT_RUN；基础容器检查、stage-only DEB、本地测试或传输
smoke 都不能冒充完整安装验收。

更新本文时，匹配 generation 15 的 native DEB、Client Control 发行物和 Echo 官方 Linux 发行物仍待发布；
clean-host 整体验收为 `NOT_RUN`。安装命令是供未来精确不可变 release pins 使用的步骤，不代表发行物或主机流程
已经通过。
