# NapCat Architecture Observations

## Purpose and research boundary

This document records only high-level architecture and externally observable
behavior needed to define an independent Cyrene QQ runtime. It is not an
implementation guide. No NapCat source, type declaration, test, native hook,
schema, generated file, algorithm, or line-by-line structure may be reused.

The read-only reference was pinned before inspection:

- repository: `NapNeko/NapCatQQ`
- commit: `3ac54c181b5e74d7acee5a62293ade88630b05ba`
- remote authority checked: repository `HEAD` and `main` resolved to that commit
- license at the pinned commit: restrictive custom Limited Redistribution
  License

The reference was inspected in a detached, temporary read-only clone. It is not
a Cyrene dependency, submodule, fork, or distribution input.

## Classification vocabulary

Every research finding below has exactly one classification:

- `OBSERVED_ARCHITECTURE`: high-level component or process organization visible
  in the pinned repository or its official documentation.
- `OBSERVED_RUNTIME_BEHAVIOR`: externally meaningful lifecycle behavior visible
  in the pinned repository or its official documentation.
- `PUBLIC_PROTOCOL_FACT`: behavior defined by public OneBot or Cyrene contracts,
  independent of NapCat's implementation.
- `INFERENCE`: a design-relevant conclusion that is not an asserted upstream
  contract.
- `UNKNOWN`: not established by the permitted research.
- `FORBIDDEN_TO_REUSE`: implementation material that may have been visible but
  must not enter Cyrene source, tests, schemas, or generated artifacts.

## Sources

- [Pinned NapCat repository](https://github.com/NapNeko/NapCatQQ/tree/3ac54c181b5e74d7acee5a62293ade88630b05ba)
- [Pinned NapCat license](https://github.com/NapNeko/NapCatQQ/blob/3ac54c181b5e74d7acee5a62293ade88630b05ba/LICENSE)
- [NapCat Shell documentation](https://napneko.github.io/guide/boot/Shell)
- [NapCat Framework documentation](https://napneko.github.io/guide/boot/Framework)
- [NapCat installation documentation](https://napneko.github.io/guide/install)
- [NapCat basic configuration documentation](https://napneko.github.io/config/basic)
- [NapCat advanced configuration documentation](https://napneko.github.io/config/advanced)
- [NapCat security documentation](https://napneko.github.io/other/security)
- [Tencent policy index](https://www.tencent.com/zh-cn/policies/)
- [Official QQ for Linux download page](https://im.qq.com/linuxqq/index.shtml)

## Audit findings

| ID | Classification | Area | High-level finding | Clean-room consequence |
| --- | --- | --- | --- | --- |
| NC-01 | `OBSERVED_ARCHITECTURE` | Monorepo structure | The pinned project is a private-package TypeScript monorepo divided into launch/supervision, native QQ access, core messaging, protocol adapters, media, persistence, Web UI, and build-support areas. | Cyrene may use its own existing Platform/Plugin boundaries; it must not reproduce the upstream package graph or names. |
| NC-02 | `OBSERVED_ARCHITECTURE` | Native loading boundary | A platform-specific boundary discovers an installed QQ runtime, determines its version/build metadata, and establishes access to QQ facilities before login/session startup. | Discovery, compatibility checking, and native access must be independent Cyrene work with documented legal authority. |
| NC-03 | `OBSERVED_RUNTIME_BEHAVIOR` | Initialization | Runtime startup is ordered: environment and version discovery precede native access; engine/login readiness precedes account login; account login precedes the account session and message listeners. | The Cyrene worker needs an explicit state machine and must reject operations before readiness. |
| NC-04 | `OBSERVED_RUNTIME_BEHAVIOR` | Login | Official project behavior includes QR login and reuse of an eligible existing account session, with asynchronous progress and failure notifications. | The first Cyrene slice may expose QR and existing-session login states, but must derive its implementation from an authorized QQ boundary and black-box evidence only. |
| NC-05 | `OBSERVED_ARCHITECTURE` | Event model | Native service notifications are normalized by a central core event layer before protocol adapters publish them. | Cyrene should normalize authorized native observations directly into `message.connector.v1`; it need not introduce an internal OneBot representation. |
| NC-06 | `OBSERVED_ARCHITECTURE` | Receive boundary | Incoming QQ messages enter through native service listeners and are then routed to higher-level adapters. | The Cyrene-owned native adapter is the receive authority for one binding and must stamp that binding before publication. |
| NC-07 | `OBSERVED_ARCHITECTURE` | Send boundary | Outbound protocol actions are translated into calls to QQ message facilities behind the native boundary. | `send_message` must resolve one binding, then use only that binding's native session. |
| NC-08 | `OBSERVED_ARCHITECTURE` | Rich media | Rich media crosses separate upload/download and media-processing boundaries and may require external media tools or platform-specific helpers. | Text-only is the first slice. Image/file support requires a later, independently specified media lifecycle and dependency review. |
| NC-09 | `OBSERVED_RUNTIME_BEHAVIOR` | Version discovery | Compatibility depends on installed QQ version/build metadata, operating-system layout, CPU architecture, and native component compatibility. | Discovery must return a compatibility report before activation; an unknown build fails closed. |
| NC-10 | `OBSERVED_ARCHITECTURE` | Supervision | Shell operation separates a supervisor from a worker/native runtime process. | Cyrene may independently use its existing worker supervision model; no upstream process protocol or retry algorithm may be reused. |
| NC-11 | `OBSERVED_RUNTIME_BEHAVIOR` | Restart lifecycle | Unexpected worker exits are observed, restart attempts are bounded, crash loops are distinguished from healthy operation, and shutdown escalates when graceful termination does not complete. | Define Cyrene-owned restart budgets, generation changes, and bounded shutdown in the runtime specification. |
| NC-12 | `OBSERVED_RUNTIME_BEHAVIOR` | Shutdown | Interrupt/termination signals enter an explicit worker shutdown path before forced termination. | CES cancellation and host shutdown must stop subscriptions, close the native session, and reap every owned process. |
| NC-13 | `OBSERVED_ARCHITECTURE` | Multi-account | Official Shell documentation advertises account selection and an external desktop manager that can manage multiple accounts. | Multi-account capability is observable, but it does not establish that one native process safely owns multiple sessions. |
| NC-14 | `INFERENCE` | Multi-account | The inspected lifecycle is consistent with one selected account and one native session per worker process. | Cyrene v0.1 adopts one binding per native session/process as the conservative isolation boundary. This is a Cyrene decision, not an upstream guarantee. |
| NC-15 | `OBSERVED_ARCHITECTURE` | Platform dependencies | Official deployment modes vary across Windows, Linux, containers, and AppImage; Linux documentation references display/session dependencies, while Windows includes platform-specific launch support. | Native process placement and GUI/session availability must be explicit installation compatibility dimensions. |
| NC-16 | `OBSERVED_RUNTIME_BEHAVIOR` | Network adapters | NapCat's public integration path commonly exposes OneBot over HTTP or WebSocket, while its Web UI can be separately configured. | This behavior is not adopted by `qqnt-direct`; the default Cyrene profile has no TCP listener and no localhost protocol hop. |
| NC-17 | `PUBLIC_PROTOCOL_FACT` | OneBot compatibility | OneBot v11 defines public action/event semantics that can be tested independently of any one implementation. | The existing `onebot-v11` profile remains a compatibility profile in the same package; it is not the internal data model for `qqnt-direct`. |
| NC-18 | `OBSERVED_RUNTIME_BEHAVIOR` | Account safety | Official NapCat security guidance warns about account restrictions, disconnects, and environment/account separation. | Live testing requires dedicated test accounts, bounded rates, explicit operator consent, and a kill switch. |
| NC-19 | `UNKNOWN` | Supported native API | The research did not establish a Tencent-published, stable native automation API for desktop QQ. | Implementation must not begin native integration until an authorized interface or written permission is established. |
| NC-20 | `UNKNOWN` | Terms authorization | The research did not establish that QQ's standard desktop license authorizes third-party native wrapping, automation, injection, or reverse engineering. | Legal/terms review is a blocking implementation gate, not a documentation caveat. |
| NC-21 | `UNKNOWN` | Session format | No reusable specification for QQ's local account/session storage was established. | Existing-session login may use only official behavior or an authorized API; Cyrene must not parse or mutate private session formats. |
| NC-22 | `UNKNOWN` | Stable event schema | No Tencent-published stable schema for QQ desktop's internal events was established. | The native adapter contract must be Cyrene-owned and version-gated; unsupported builds fail closed. |

## Material forbidden to reuse

| ID | Classification | Material | Rule |
| --- | --- | --- | --- |
| FR-01 | `FORBIDDEN_TO_REUSE` | Source code, control flow, algorithms, constants, exact retry timing, or exact startup sequences from the pinned repository | Must not be copied, translated, mechanically transformed, or reconstructed line by line. |
| FR-02 | `FORBIDDEN_TO_REUSE` | Type declarations, interfaces, event shapes, configuration schemas, generated protocol files, or tests | Must not appear in Cyrene source or test fixtures. |
| FR-03 | `FORBIDDEN_TO_REUSE` | Native hooks, binary loaders, packet implementations, private protocol details, symbol names, or platform bypass techniques | Must not be used as design or implementation input. |
| FR-04 | `FORBIDDEN_TO_REUSE` | Native binaries, packaged assets, vendored dependencies, or release archives | Must not be redistributed, linked, loaded, or declared as a Cyrene dependency. |
| FR-05 | `FORBIDDEN_TO_REUSE` | NapCat process IPC, named-pipe protocol, supervisor messages, filesystem layout, or internal module naming | Cyrene must define its own worker protocol and lifecycle. |
| FR-06 | `FORBIDDEN_TO_REUSE` | NapCat configuration or account data | Must not be imported, migrated, read, or reinterpreted by `qqnt-direct`. |

## Research conclusion

The permitted observations support only broad lifecycle requirements: version
discovery, ordered login/session initialization, event-driven messaging,
process supervision, and strict account isolation. They do not supply an
implementation boundary that Cyrene is authorized to reuse. The clean-room
runtime must therefore be specified from Cyrene contracts, Tencent-authorized
interfaces, public protocol facts, and independently created black-box tests.
---
<!-- Chinese Translation / 中文翻译 -->

# NapCat 架构观察

## 目的与研究边界

本文只记录定义独立 Cyrene QQ 运行时所需的高层架构和外部可观察行为。本文不是实现指南。不得复用任何 NapCat 源码、类型声明、测试、原生 Hook、schema、生成文件、算法或逐行结构。

在检查前已固定只读参考版本：

- 仓库：`NapNeko/NapCatQQ`
- 提交：`3ac54c181b5e74d7acee5a62293ade88630b05ba`
- 已核对的远端权威：仓库 `HEAD` 和 `main` 均指向该提交
- 固定提交的许可证：具有限制的自定义 Limited Redistribution License

该参考版本是在临时、脱离分支且只读的克隆中检查的。它不是 Cyrene 依赖、子模块、Fork 或分发输入。

## 分类词汇

以下每项研究结论都且仅有一个分类：

- `OBSERVED_ARCHITECTURE`（**观察到的架构**）：固定仓库或其官方文档中可见的高层组件 / 进程组织。
- `OBSERVED_RUNTIME_BEHAVIOR`（**观察到的运行时行为**）：固定仓库或其官方文档中可见、对外有意义的生命周期行为。
- `PUBLIC_PROTOCOL_FACT`（**公开协议事实**）：由公开 OneBot 或 Cyrene 合约定义、独立于 NapCat 实现的行为。
- `INFERENCE`（**推断**）：与设计相关、但不属于上游明确契约的结论。
- `UNKNOWN`（**未知**）：经允许的研究尚未确定的事项。
- `FORBIDDEN_TO_REUSE`（**禁止复用**）：可能已在检查过程中见到、但不得进入 Cyrene 源码、测试、schema 或生成制品的实现材料。

## 来源

- [固定的 NapCat 仓库](https://github.com/NapNeko/NapCatQQ/tree/3ac54c181b5e74d7acee5a62293ade88630b05ba)
- [固定版本的 NapCat 许可证](https://github.com/NapNeko/NapCatQQ/blob/3ac54c181b5e74d7acee5a62293ade88630b05ba/LICENSE)
- [NapCat Shell 文档](https://napneko.github.io/guide/boot/Shell)
- [NapCat Framework 文档](https://napneko.github.io/guide/boot/Framework)
- [NapCat 安装文档](https://napneko.github.io/guide/install)
- [NapCat 基础配置文档](https://napneko.github.io/config/basic)
- [NapCat 高级配置文档](https://napneko.github.io/config/advanced)
- [NapCat 安全文档](https://napneko.github.io/other/security)
- [腾讯政策索引](https://www.tencent.com/zh-cn/policies/)
- [QQ Linux 官方下载页面](https://im.qq.com/linuxqq/index.shtml)

## 审计发现

| ID | 分类 | 领域 | 高层发现 | 洁净室影响 |
|---|---|---|---|---|
| NC-01 | `OBSERVED_ARCHITECTURE` | Monorepo 结构 | 固定项目是使用私有包的 TypeScript Monorepo，分为启动 / 监督、原生 QQ 访问、核心消息、协议适配器、媒体、持久化、Web UI 和构建支持等区域。| Cyrene 可以使用已有 Platform/Plugin 边界；不得重现上游包图或命名。|
| NC-02 | `OBSERVED_ARCHITECTURE` | 原生加载边界 | 平台专属边界会发现已安装的 QQ 运行时、确定其版本 / 构建元数据，并在登录 / 会话启动前建立对 QQ 功能的访问。| 发现、兼容性检查和原生访问必须是 Cyrene 独立完成的工作，并有书面法律授权。|
| NC-03 | `OBSERVED_RUNTIME_BEHAVIOR` | 初始化 | 运行时按顺序启动：环境和版本发现先于原生访问；引擎 / 登录就绪先于账号登录；账号登录先于账号会话和消息监听器。| Cyrene Worker 需要显式状态机，并须拒绝就绪前的操作。|
| NC-04 | `OBSERVED_RUNTIME_BEHAVIOR` | 登录 | 官方项目行为包括 QR 登录和复用符合条件的现有账号会话，并提供异步进度和失败通知。| Cyrene 首个切片可提供 QR 和现有会话登录状态，但实现必须只基于获授权的 QQ 边界及黑盒证据。|
| NC-05 | `OBSERVED_ARCHITECTURE` | 事件模型 | 原生服务通知由中央核心事件层规范化，然后再由协议适配器发布。| Cyrene 应将经过授权的原生观察直接规范化为 `message.connector.v1`；无需引入内部 OneBot 表示。|
| NC-06 | `OBSERVED_ARCHITECTURE` | 接收边界 | 入站 QQ 消息经由原生服务监听器进入，然后路由至较高层适配器。| Cyrene 自有的原生适配器是单个绑定的接收权威，且发布前必须标记该绑定。|
| NC-07 | `OBSERVED_ARCHITECTURE` | 发送边界 | 出站协议操作会转换为原生边界后的 QQ 消息功能调用。| `send_message` 必须解析出一个绑定，并且只使用该绑定的原生会话。|
| NC-08 | `OBSERVED_ARCHITECTURE` | 富媒体 | 富媒体会经过独立的上传 / 下载和媒体处理边界，可能需要外部媒体工具或平台专属 helper。| 首个切片仅支持文本。图像 / 文件支持需在之后单独定义媒体生命周期并审查依赖。|
| NC-09 | `OBSERVED_RUNTIME_BEHAVIOR` | 版本发现 | 兼容性取决于已安装 QQ 的版本 / 构建元数据、操作系统布局、CPU 架构和原生组件兼容性。| 激活前发现流程必须返回兼容性报告；未知构建版本必须快速失败。|
| NC-10 | `OBSERVED_ARCHITECTURE` | 监督 | Shell 的运行方式将监督器与 Worker / 原生运行时进程分离。| Cyrene 可以独立使用现有 Worker 监督模型；不得复用上游进程协议或重试算法。|
| NC-11 | `OBSERVED_RUNTIME_BEHAVIOR` | 重启生命周期 | 系统会观测 Worker 意外退出，有界地尝试重启，区分崩溃循环与健康运行，并在平稳终止未完成时升级关闭方式。| 在运行时规范中定义 Cyrene 自有的重启预算、代次变更和有界关闭。|
| NC-12 | `OBSERVED_RUNTIME_BEHAVIOR` | 关闭 | 中断 / 终止信号会先进入 Worker 的显式关闭路径，然后才强制终止。| CES 取消和主机关闭必须停止订阅、关闭原生会话并回收所有所属进程。|
| NC-13 | `OBSERVED_ARCHITECTURE` | 多账号 | 官方 Shell 文档宣称支持账号选择，并提供可管理多个账号的外部桌面管理器。| 可观察到多账号能力，但这不能证明一个原生进程能够安全管理多个会话。|
| NC-14 | `INFERENCE` | 多账号 | 检查到的生命周期与每个 Worker 进程使用一个选定账号和一个原生会话的设计相符。| Cyrene v0.1 将每个绑定对应一个原生会话 / 进程作为保守隔离边界。这是 Cyrene 的决定，并非上游保证。|
| NC-15 | `OBSERVED_ARCHITECTURE` | 平台依赖 | 官方部署模式因 Windows、Linux、容器和 AppImage 而异；Linux 文档提到显示 / 会话依赖，Windows 包含平台专属启动支持。| 原生进程放置和 GUI / 会话可用性必须作为明确的安装兼容维度。|
| NC-16 | `OBSERVED_RUNTIME_BEHAVIOR` | 网络适配器 | NapCat 的公开集成路径通常通过 HTTP 或 WebSocket 暴露 OneBot，Web UI 则可单独配置。| `qqnt-direct` 不采用此行为；Cyrene 默认配置档不使用 TCP 监听器，也不经 localhost 协议转发。|
| NC-17 | `PUBLIC_PROTOCOL_FACT` | OneBot 兼容性 | OneBot v11 定义公开的 action / event 语义，可独立于任何单一实现进行测试。| 现有 `onebot-v11` 配置档仍作为同一包中的兼容配置档；它不是 `qqnt-direct` 的内部数据模型。|
| NC-18 | `OBSERVED_RUNTIME_BEHAVIOR` | 账号安全 | NapCat 官方安全指引警告账号可能受到限制、断连，并要求隔离环境与账号。| 在线测试需要专用测试账号、有界速率、运营方明确同意和 kill switch。|
| NC-19 | `UNKNOWN` | 支持的原生 API | 研究未能确认腾讯发布了面向桌面 QQ 的稳定原生自动化 API。| 获得授权接口或书面许可之前，不得开始原生集成。|
| NC-20 | `UNKNOWN` | 条款授权 | 研究未能确认 QQ 标准桌面许可是否授权第三方原生包装、自动化、注入或逆向工程。| 法律 / 条款审查是实现阻塞门禁，不能只作为文档注意事项。|
| NC-21 | `UNKNOWN` | 会话格式 | 尚未确定 QQ 本地账号 / 会话存储的可复用规范。| 只有官方行为或授权 API 才能支持现有会话登录；Cyrene 不得解析或修改私有会话格式。|
| NC-22 | `UNKNOWN` | 稳定事件 schema | 尚未发现腾讯发布的、针对 QQ 桌面内部事件的稳定 schema。| 原生适配器合约必须由 Cyrene 自有并设置版本门禁；不支持的构建版本须快速失败。|

## 禁止复用的材料

| ID | 分类 | 材料 | 规则 |
|---|---|---|---|
| FR-01 | `FORBIDDEN_TO_REUSE` | 固定仓库中的源码、控制流、算法、常量、精确重试间隔或精确启动顺序 | 不得复制、翻译、机械转换或逐行重建。|
| FR-02 | `FORBIDDEN_TO_REUSE` | 类型声明、接口、事件结构、配置 schema、生成协议文件或测试 | 不得出现在 Cyrene 源码或测试夹具中。|
| FR-03 | `FORBIDDEN_TO_REUSE` | 原生 Hook、二进制加载器、数据包实现、私有协议细节、符号名或平台绕过技术 | 不得作为设计或实现输入。|
| FR-04 | `FORBIDDEN_TO_REUSE` | 原生二进制文件、打包资源、供应商依赖或发行归档 | 不得再分发、链接、加载或声明为 Cyrene 依赖。|
| FR-05 | `FORBIDDEN_TO_REUSE` | NapCat 进程 IPC、命名管道协议、监督器消息、文件系统布局或内部模块命名 | Cyrene 必须定义自己的 Worker 协议和生命周期。|
| FR-06 | `FORBIDDEN_TO_REUSE` | NapCat 配置或账号数据 | `qqnt-direct` 不得导入、迁移、读取或重新解释这些数据。|

## 研究结论

允许采用的观察只能支持宽泛的生命周期要求：版本发现、按序初始化登录 / 会话、事件驱动消息处理、进程监督和严格的账号隔离。这些观察并未提供 Cyrene 获准复用的实现边界。因此，洁净室运行时必须基于 Cyrene 合约、腾讯授权接口、公开协议事实和独立创建的黑盒测试来定义。
