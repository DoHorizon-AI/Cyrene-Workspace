# QQNT Direct Clean-room Boundary

## Decision

Cyrene may design a portless QQ connector profile, but it may not reuse or
depend on NapCat. Native implementation work is gated on confirming a lawful,
documented interface to the official locally installed QQ runtime.

This boundary applies to source, tests, build scripts, generated files,
artifacts, dependency locks, package metadata, and contributor provenance.

## Allowed design inputs

- Cyrene-owned contracts: `message.connector.v1`, Capability Execution Service,
  Capability Binding, worker protocol, application events, Package Spec v0.1,
  PluginManifest, PluginSetLock, and PluginStore.
- Tencent-published QQ installation, version, account, and service
  documentation, subject to its terms.
- A Tencent-authorized native or automation interface and its documentation.
- Public OneBot v11 protocol behavior for the existing compatibility profile.
- Independently authored black-box tests using dedicated accounts and an
  official QQ installation.
- High-level observations in `NAPCAT_ARCHITECTURE_OBSERVATIONS.md`, limited to
  lifecycle requirements and risk identification.

## Prohibited inputs and outputs

- No NapCat code, declarations, tests, schemas, generated files, binaries,
  assets, hooks, packet logic, IPC, or configuration data.
- No copied or translated implementation, including mechanical rewrites in
  another language.
- No reconstruction of exact internal module boundaries, startup logic,
  retries, native loading, event shapes, or media pipelines.
- No NapCat package, source checkout, release, container, or runtime dependency.
- No dynamic download of NapCat during build, install, activation, tests, or
  recovery.
- No use of private QQ protocols, injected hooks, memory modification, or
  reverse engineering unless a specific legal review and written authority
  approves the exact technique.
- No import of existing QQ account/session files except through behavior
  explicitly supported by the official runtime or an authorized interface.

## Contributor provenance protocol

The author of the NapCat observation document is considered research-exposed
and must not author the native wrapper or native loading implementation.

Before implementation begins:

1. nominate an implementation owner who has not inspected NapCat source;
2. provide only these clean-room documents, Cyrene contracts, public protocol
   documents, and approved Tencent interface documentation;
3. record each implementation input by URL, version, license, and approval;
4. require every native-boundary change to include an origin/provenance note;
5. review diffs for suspicious naming, structure, constants, signatures, and
   dependency overlap;
6. retain black-box test recordings and environment manifests without account
   secrets or private user data;
7. reject any contribution whose independent origin cannot be demonstrated.

Automated similarity checks can support review but do not replace provenance or
legal review. NapCat source must not be placed in CI, build caches, test images,
or comparison jobs.

## QQ terms gate

[Tencent's policy index](https://www.tencent.com/zh-cn/policies/) identifies the
[QQ Software License and Service Agreement](https://rule.tencent.com/rule/preview/46a15f24-e42c-4cb6-a308-2347139b1201)
as the governing product agreement. Tencent's published global service terms
also reserve software rights and restrict reverse engineering except where
applicable law or prior consent permits it. These sources do not, by themselves,
authorize the proposed native wrapper. This document records an engineering
gate and is not legal advice.

Required approval record before native implementation:

- exact QQ product/version and operating system;
- exact interface used to load, launch, or automate it;
- whether process injection, patching, private symbols, or binary modification
  occurs;
- distribution model for the Cyrene package and any helper binary;
- account class and automation use case;
- counsel/owner decision and any Tencent permission;
- permitted test environments and rate limits.

If the approved interface requires injection, patching, private protocol
reconstruction, or redistribution of QQ native libraries, the current design
must return to architecture review rather than treating those techniques as an
implementation detail.

## Technical boundary

The default runtime chain is:

```text
Product
  -> CapabilityExecutionService
  -> binding-scoped canonical worker over stdio
  -> Cyrene QQ connector worker
  -> optional Cyrene native helper over inherited stdio
  -> official, locally installed QQ runtime
```

No stage opens an HTTP, WebSocket, or other TCP listening port. If the native
helper cannot inherit stdio, the only allowed fallback is an owner-only Unix
domain socket on POSIX or an owner-restricted named pipe on Windows. The helper
must not be reachable beyond the binding's process tree.

The native helper is a narrow authorized adapter. It does not own Product
policy, OneBot compatibility, session history, memory, moderation, routing, or
application persistence.

## Package boundary

`qqnt-direct` ships in the same immutable installable connector package lineage
as the existing official OneBot v11 connector. It is a runtime profile, not a
second catalog or a second capability.

The package may share immutable code and digest-addressed dependency runtime
across bindings. Binding configuration, secret references, account state,
native session, worker process, event stream, and outbound route are never
shared.

The package must contain no official QQ binary or library unless Tencent has
explicitly authorized redistribution. Normal operation discovers a separately
installed official QQ runtime and verifies compatibility before activation.

## Clean-room acceptance gates

- dependency graph contains no NapCat package, repository URL, binary, or
  derived artifact;
- source and generated-output review finds no prohibited upstream material;
- package build succeeds without a NapCat checkout or network access to NapCat;
- runtime starts only from the installed package and an official local QQ
  installation;
- legal/terms approval identifies the exact native interface;
- unsupported QQ builds fail closed before account state is touched;
- two bindings have separate process trees, data directories, secret handles,
  sessions, and event routes;
- shutdown leaves no child process, IPC endpoint, lock, or temporary secret;
- port scan proves zero connector-owned listening TCP ports.

## Current boundary status

- NapCat code reuse: **prohibited and absent by design**.
- NapCat runtime dependency: **prohibited and absent by design**.
- Portless process architecture: **frozen**.
- Native implementation authorization: **not yet established**.
---
<!-- Chinese Translation / 中文翻译 -->

# QQNT Direct 洁净室边界

## 决策

Cyrene 可以设计无端口的 QQ Connector 配置档，但不得复用或依赖 NapCat。原生实现工作必须先确认存在针对官方本地安装 QQ 运行时的合法、已记录接口，之后才能开始。

此边界适用于源码、测试、构建脚本、生成文件、制品、依赖锁、包元数据和贡献者来源记录。

## 允许使用的设计输入

- Cyrene 自有合约：`message.connector.v1`、Capability Execution Service、Capability Binding、Worker 协议、应用事件、Package Spec v0.1、PluginManifest、PluginSetLock 和 PluginStore。
- 腾讯发布的 QQ 安装、版本、账号和服务文档，并遵守其条款。
- 腾讯授权的原生接口或自动化接口及其文档。
- 现有兼容配置档公开的 OneBot v11 协议行为。
- 使用专用账号和官方 QQ 安装独立编写的黑盒测试。
- `NAPCAT_ARCHITECTURE_OBSERVATIONS.md` 中的高层观察，但仅限生命周期要求与风险识别。

## 禁止使用的输入与产物

- 不得使用任何 NapCat 代码、声明、测试、schema、生成文件、二进制文件、资源、Hook、数据包逻辑、IPC 或配置数据。
- 不得复制或翻译其实现，包括以其他语言进行机械改写。
- 不得重建其精确的内部模块边界、启动逻辑、重试机制、原生加载流程、事件结构或媒体管线。
- 不得依赖 NapCat 包、源码检出、发行版、容器或运行时。
- 不得在构建、安装、激活、测试或恢复期间动态下载 NapCat。
- 除非特定法律审查和书面授权批准了具体技术，否则不得使用 QQ 私有协议、注入式 Hook、内存修改或逆向工程。
- 除非官方运行时或授权接口明确支持相应行为，否则不得导入现有 QQ 账号 / 会话文件。

## 贡献者来源协议

《NapCat 架构观察》文档的作者被视为接触过研究材料，因此不得编写原生包装层或原生加载实现。

开始实现前必须：

1. 指定一名未检查过 NapCat 源码的实现负责人；
2. 只向其提供这些洁净室文档、Cyrene 合约、公开协议文档和经过批准的腾讯接口文档；
3. 按 URL、版本、许可和审批记录每一项实现输入；
4. 要求每一项原生边界变更都附带来源 / 血缘说明；
5. 审查差异，检查可疑的命名、结构、常量、签名和依赖重叠；
6. 保留黑盒测试记录和环境清单，但不得包含账号秘密或私人用户数据；
7. 拒绝任何无法证明来源独立的贡献。

自动相似度检查可辅助审查，但不能取代来源核查或法律审查。CI、构建缓存、测试镜像和代码比对任务中都不得放置 NapCat 源码。

## QQ 条款门禁

[腾讯政策索引](https://www.tencent.com/zh-cn/policies/) 将 [《QQ 软件许可及服务协议》](https://rule.tencent.com/rule/preview/46a15f24-e42c-4cb6-a308-2347139b1201)列为适用的产品协议。腾讯发布的全球服务条款也保留软件相关权利，并限制逆向工程，除非适用法律或事先许可允许此类行为。仅凭这些来源并不能授权拟议的原生包装层。本文记录的是工程门禁，不构成法律意见。

原生实现开始前必须记录以下批准信息：

- 确切的 QQ 产品 / 版本和操作系统；
- 用于加载、启动或自动化 QQ 的确切接口；
- 是否会进行进程注入、修补、使用私有符号或修改二进制文件；
- Cyrene 包及任何辅助二进制文件的分发模式；
- 账号类别和自动化使用场景；
- 法务 / 负责人决策以及腾讯许可（如有）；
- 允许使用的测试环境和速率限制。

如果获批接口要求注入、修补、重建私有协议，或重新分发 QQ 原生库，则当前设计必须返回架构审查，不能将这些技术视为普通实现细节。

## 技术边界

默认运行时链路为：

```text
Product
  -> CapabilityExecutionService
  -> 绑定作用域内通过 stdio 通信的规范 worker
  -> Cyrene QQ Connector worker
  -> 可选的 Cyrene 原生 helper（通过继承的 stdio 通信）
  -> 官方本地安装的 QQ 运行时
```

任何阶段都不得打开 HTTP、WebSocket 或其他 TCP 监听端口。如果原生 helper 无法继承 stdio，唯一允许的回退方式是在 POSIX 系统上使用仅所有者可访问的 Unix domain socket，或在 Windows 上使用受所有者权限限制的命名管道。helper 不得从绑定所属进程树以外访问。

原生 helper 是范围狭窄、经过授权的适配器。它不拥有 Product 策略、OneBot 兼容性、会话历史、记忆、内容审核、路由或应用持久化。

## 包边界

`qqnt-direct` 与现有官方 OneBot v11 Connector 属于同一条不可变、可安装的 Connector 包血缘。它是一种运行时配置档，不是第二个目录或第二种能力。

不同绑定可以共用不可变代码和按摘要寻址的依赖运行时，但不得共享绑定配置、秘密引用、账号状态、原生会话、Worker 进程、事件流或出站路由。

除非腾讯明确授权再分发，包中不得包含官方 QQ 二进制文件或库。正常运行时会发现单独安装的官方 QQ 运行时，并在激活前验证兼容性。

## 洁净室验收门禁

- 依赖图中不包含 NapCat 包、仓库 URL、二进制文件或衍生产物；
- 源码和生成输出审查未发现被禁止的上游材料；
- 无需 NapCat 源码检出或访问 NapCat 网络即可成功构建软件包；
- 运行时只能从已安装的软件包和官方本地 QQ 安装启动；
- 法律 / 条款审批记录明确指出所使用的原生接口；
- 不支持的 QQ 构建必须在触及账号状态之前快速失败；
- 两个绑定拥有相互隔离的进程树、数据目录、秘密句柄、会话和事件路由；
- 关闭后不留下子进程、IPC 端点、锁或临时秘密；
- 端口扫描确认 Connector 自有的 TCP 监听端口数为零。

## 当前边界状态

- 复用 NapCat 代码：**禁止，设计中也未采用**。
- 依赖 NapCat 运行时：**禁止，设计中也未采用**。
- 无端口进程架构：**已冻结**。
- 原生实现授权：**尚未建立**。
