# QQNT Direct Implementation Plan

## Scope

This plan defines the first independently implementable slice after legal and
native-interface approval. It does not authorize implementation and does not
include code from the research reference.

## Entry gates

Implementation starts only when all are true:

1. QQ product terms and the exact native/automation technique have written
   approval from the responsible owner or counsel.
2. A Tencent-published or explicitly authorized interface is identified. The
   plan must return to architecture review if the technique requires injection,
   patching, private protocol reconstruction, or QQ binary redistribution.
3. The implementation owner has not inspected NapCat source and accepts the
   provenance protocol in `CLEAN_ROOM_BOUNDARY.md`.
4. Exact supported QQ version/build, operating system, CPU architecture, and
   installation source are pinned.
5. Dedicated test accounts and an isolated test environment are available.
6. The existing package lifecycle and P2.5 vertical gate pass at current
   canonical repository SHAs.

## Minimal slice

### Slice 1: package/profile projection

- version the existing official connector package rather than creating a
  second package registry;
- add `qq` family, `qqnt-direct` runtime profile, and `onebot-v11`
  compatibility-profile metadata through current PluginManifest and Package
  Spec authorities;
- keep the existing OneBot transport profile functional;
- update the configuration schema so direct and OneBot fields are mutually
  exclusive;
- produce one immutable artifact, exact dependency lock, digest, provenance,
  and SBOM hook;
- prove no QQ or NapCat binaries are bundled without explicit distribution
  authority.

### Slice 2: official QQ installation discovery

- implement independent platform adapters for supported installation layouts
  using public OS/package metadata or operator-provided paths;
- canonicalize executable and data paths;
- detect zero, one, or multiple installations deterministically;
- collect QQ version/build, OS, and architecture without modifying the
  installation;
- reject unknown or unapproved builds before loading the native boundary.

Initial support should select one host/QQ combination only. Additional layouts
are separate compatibility work, not heuristic fallback.

### Slice 3: Cyrene native wrapper boundary

- define the Cyrene-owned versioned stdio helper protocol;
- implement the smallest authorized adapter for runtime initialization, login,
  session readiness, text receive, text send, and shutdown;
- place the helper beside the official runtime on the native OS;
- use inherited stdio by default; use owner-only UDS/named pipe only if proven
  necessary;
- forbid TCP listeners and enforce this in tests;
- keep native implementation in a separately reviewed package area with a
  provenance manifest.

### Slice 4: login and session

- support reuse of an officially recognized existing session in the binding's
  private data directory;
- otherwise expose QR login progress through a bounded administrative event;
- validate the resulting QQ account against binding configuration;
- start one native session for one binding generation;
- do not parse or migrate private QQ/NapCat session formats;
- do not add password, captcha bypass, device spoofing, or risk-control
  circumvention to the minimal slice.

### Slice 5: direct message mapping

- normalize private text receive directly to `message.connector.v1`;
- normalize group text receive directly to `message.connector.v1`;
- implement `send_message` for private and group text;
- preserve binding, account, conversation, sender, message, and generation
  identity separately;
- implement deadlines, cancellation, correlation, and generic CES errors;
- do not round-trip internal data through OneBot JSON.

### Slice 6: supervision and isolation

- activate one worker/helper/native session process tree per binding;
- create binding-private account data, temporary, log, and IPC roots;
- share only immutable package and locked dependency runtime by digest;
- implement bounded restart/backoff and a binding-local crash circuit;
- preserve binding ID and change generation on restart;
- drain and reap every process and IPC resource on shutdown.

## Test strategy

### Hermetic tests

- package metadata and profile selection conformance;
- configuration mutual exclusion and secret-reference handling;
- installation discovery over synthetic filesystem/package-manager fixtures;
- helper protocol framing, negotiation, correlation, cancellation, and malformed
  input using an independently authored fake native counterpart;
- direct canonical mapping for private/group text;
- no OneBot serialization in `qqnt-direct` execution;
- zero listening ports;
- process-tree restart, timeout, cancellation, and shutdown cleanup;
- static dependency/provenance scan proving zero NapCat material.

### Binding isolation test

Run `qq-main` and `qq-secondary` concurrently with overlapping native message,
conversation, and trace-like identifiers:

- A receive reaches only A subscription;
- B receive reaches only B subscription;
- Invoke A reaches only A native counterpart;
- Invoke B reaches only B native counterpart;
- account data, secrets, correlation, queues, and logs remain separate;
- restart A changes only A generation;
- immutable package code and dependency runtime are reused by digest.

### Authorized official-runtime smoke

On the approved host and QQ build:

- discover the official installation;
- existing-session or QR login with a dedicated test account;
- receive private text;
- receive group text;
- send private text;
- send group text;
- reconnect after controlled process interruption;
- verify binding/account identity after restart;
- shut down with no orphan process, IPC endpoint, lock, or listening port.

This smoke is not replaceable by a NapCat deployment. NapCat is neither the
runtime under test nor a fixture.

## P0/P2.5 harness extension

Extend the committed three-repository vertical harness instead of creating a
parallel framework:

1. build and install the new version of the same official connector package;
2. activate two `qqnt-direct` bindings from the installed artifact;
3. provide an independently authored fake native helper counterpart for CI;
4. run real CES Invoke/Subscribe, AstrBot host, and disposable pgvector;
5. assert database/state/outbound isolation for overlapping identifiers;
6. repeat offline reinstall, corruption rejection, upgrade, rollback, and
   cleanup checks;
7. assert the source checkout is unavailable at runtime;
8. assert no OneBot endpoint and no TCP listener exist in direct mode.

The authorized official-runtime smoke remains a separate protected/nightly gate
because it requires QQ installation, terms-approved execution, and test-account
credentials.

## Repository ownership

- Cyrene-Platform: no QQ semantics; only existing generic CES/binding/worker
  behavior unless a generic defect is proven.
- Cyrene-Plugins-Official: package profiles, connector worker, native adapter,
  protocol mapping, package tests, and provenance.
- AstrBot-Rev: Product binding configuration and Product/session policy only;
  no native QQ fallback.
- Cyrene-Workspace: package/profile governance, clean-room evidence, accepted
  baselines, and repeatable vertical CI orchestration.

Each repository change uses its own task branch and canonical workflow.

## Stop conditions

- no written authority for the exact QQ native technique;
- implementation contributor has unbounded NapCat source exposure;
- required behavior can be achieved only through prohibited copying, injection,
  patching, private-protocol reconstruction, or unauthorized redistribution;
- unsupported QQ build is being accepted through heuristic fallback;
- direct mode opens a TCP listener or falls back to OneBot;
- one binding can observe another binding's account, events, secrets, state, or
  outbound route;
- package lifecycle needs `pip install latest` or a source-tree fallback.

## Definition of done for the first slice

- all entry gates recorded and approved;
- immutable package installs through Package Spec v0.1 lifecycle;
- clean host has no preinstalled connector or NapCat material;
- authorized official QQ runtime is discovered and version-gated;
- existing-session or QR login succeeds for one binding;
- private/group text receive and send pass;
- two-binding isolation passes;
- restart preserves binding and changes generation;
- stdio is the default IPC and connector-owned TCP listeners remain zero;
- P2.5-derived CI gate and protected official-runtime smoke pass;
- cleanup proves no orphan process, IPC endpoint, port, container, or temporary
  package/account state;
- clean-room provenance review is signed off.
---
<!-- Chinese Translation / 中文翻译 -->

# QQNT Direct 实施计划

## 范围

本计划定义法律和原生接口获批后，首个可独立实施的切片。本计划不授权开始实现，也不包含研究参考资料中的代码。

## 准入门禁

只有以下条件全部满足后才能开始实施：

1. 负责的 owner 或法律顾问已书面批准 QQ 产品条款以及确切的原生 / 自动化技术。
2. 已确定腾讯发布或明确授权的接口。如果该技术要求注入、修补、重建私有协议或再分发 QQ 二进制文件，计划必须退回架构审查。
3. 实施负责人没有检查过 NapCat 源码，并接受 `CLEAN_ROOM_BOUNDARY.md` 中的来源协议。
4. 已固定确切支持的 QQ 版本 / 构建、操作系统、CPU 架构和安装来源。
5. 已准备专用测试账号和隔离测试环境。
6. 现有包生命周期和 P2.5 垂直门禁已在当前规范仓库 SHA 上通过。

## 最小切片

### 切片 1：包 / 配置档投影

- 对现有官方 Connector 包进行版本化，而不是创建第二个包 registry；
- 通过当前 PluginManifest 和 Package Spec 权威添加 `qq` family、`qqnt-direct` runtime profile 和 `onebot-v11` compatibility profile 元数据；
- 保持现有 OneBot 传输配置档可用；
- 更新配置 schema，使 direct 与 OneBot 字段互斥；
- 生成一个不可变制品、精确依赖锁、摘要、来源记录和 SBOM hook；
- 证明未在缺少明确分发授权时捆绑 QQ 或 NapCat 二进制文件。

### 切片 2：发现官方 QQ 安装

- 使用公开的操作系统 / 包管理元数据或运营方提供的路径，为受支持的安装布局分别实现独立的平台适配器；
- 规范化可执行文件和数据路径；
- 以确定性方式检测零个、一个或多个安装；
- 在不修改安装内容的前提下收集 QQ 版本 / 构建、操作系统和架构信息；
- 在加载原生边界前拒绝未知或未经批准的构建版本。

初始支持应只选择一种主机 / QQ 组合。其他布局属于单独的兼容性工作，不得使用启发式回退。

### 切片 3：Cyrene 原生包装层边界

- 定义 Cyrene 自有的版本化 stdio helper 协议；
- 为运行时初始化、登录、会话就绪、接收文本、发送文本和关闭实现范围最小的授权适配器；
- 在原生操作系统上、官方运行时附近放置 helper；
- 默认使用继承的 stdio；只有证明确有必要时才使用仅所有者可访问的 UDS / 命名管道；
- 禁止 TCP 监听，并通过测试强制执行；
- 将原生实现放在单独审查的包区域，并附带来源 manifest。

### 切片 4：登录与会话

- 支持在绑定私有数据目录中复用官方认可的现有会话；
- 否则通过有界的管理事件展示 QR 登录进度；
- 根据绑定配置校验登录得到的 QQ 账号；
- 每个绑定代次只启动一个原生会话；
- 不解析或迁移 QQ/NapCat 私有会话格式；
- 最小切片中不得增加密码、验证码绕过、设备伪装或规避风控的功能。

### 切片 5：直接消息映射

- 将私聊文本接收直接规范化为 `message.connector.v1`；
- 将群聊文本接收直接规范化为 `message.connector.v1`；
- 实现私聊和群聊文本的 `send_message`；
- 分别保留 binding、账号、会话、发送者、消息和代次身份；
- 实现截止时间、取消、关联和通用 CES 错误；
- 不通过 OneBot JSON 对内部数据进行往返转换。

### 切片 6：监督与隔离

- 每个绑定分别激活一个 worker/helper/原生会话进程树；
- 为账号数据、临时文件、日志和 IPC 根目录创建绑定私有路径；
- 仅按摘要共享不可变包和已锁定的依赖运行时；
- 实现有界重启 / 退避策略和绑定本地崩溃熔断器；
- 重启时保留 Binding ID 并更改代次；
- 关闭时排空并回收全部进程和 IPC 资源。

## 测试策略

### Hermetic 测试

- 包元数据和配置档选择一致性测试；
- 配置互斥及秘密引用处理；
- 使用合成文件系统 / 包管理器夹具测试安装发现；
- 使用独立编写的原生 fake 对端测试 helper 协议帧格式、协商、关联、取消和畸形输入；
- 测试私聊 / 群聊文本到规范消息的直接映射；
- 确认 `qqnt-direct` 执行过程中不序列化为 OneBot；
- 确认监听端口数为零；
- 测试进程树重启、超时、取消和关闭清理；
- 静态依赖 / 来源扫描，证明没有 NapCat 材料。

### 绑定隔离测试

并发运行 `qq-main` 与 `qq-secondary`，并让它们使用相互重叠的原生消息、会话和类似 trace 的标识：

- A 的接收事件只到达 A 的订阅；
- B 的接收事件只到达 B 的订阅；
- A 的 Invoke 只到达 A 的原生对端；
- B 的 Invoke 只到达 B 的原生对端；
- 账号数据、秘密、关联信息、队列和日志均保持隔离；
- 重启 A 只更改 A 的代次；
- 按摘要复用不可变包代码和依赖运行时。

### 经授权的官方运行时冒烟测试

在获批的主机和 QQ 构建版本上：

- 发现官方安装；
- 使用专用测试账号通过已有会话或 QR 登录；
- 接收私聊文本；
- 接收群聊文本；
- 发送私聊文本；
- 发送群聊文本；
- 在受控进程中断后重新连接；
- 重启后验证绑定 / 账号身份；
- 关闭后确认没有孤儿进程、IPC 端点、锁或监听端口。

此冒烟测试不能由 NapCat 部署替代。NapCat 既不是被测运行时，也不是测试夹具。

## P0/P2.5 Harness 扩展

扩展已提交的三仓垂直 Harness，而不是另建一套并行框架：

1. 构建并安装同一官方 Connector 包的新版本；
2. 从已安装制品激活两个 `qqnt-direct` 绑定；
3. 为 CI 提供独立编写的 fake 原生 helper 对端；
4. 运行真实 CES Invoke/Subscribe、AstrBot host 和临时 pgvector；
5. 对重叠标识断言数据库 / 状态 / 出站隔离；
6. 重复验证离线重装、损坏拒绝、升级、回滚和清理；
7. 断言运行时无法访问源码检出；
8. 断言 Direct 模式下不存在 OneBot endpoint 和 TCP 监听器。

经授权的官方运行时冒烟测试仍是独立的受保护 / nightly 门禁，因为它需要安装 QQ、获条款批准的执行环境和测试账号凭据。

## 仓库职责

- **Cyrene-Platform**：不引入 QQ 语义；除非证明存在通用缺陷，否则仅使用现有通用 CES / binding / worker 行为。
- **Cyrene-Plugins-Official**：负责包配置档、Connector worker、原生适配器、协议映射、包测试和来源记录。
- **AstrBot-Rev**：仅负责 Product 绑定配置与 Product / 会话策略；不得实现原生 QQ 回退。
- **Cyrene-Workspace**：负责包 / 配置档治理、洁净室证据、已接受基线和可重复的垂直 CI 编排。

每个仓库的变更都使用各自的任务分支和规范工作流。

## 停止条件

- 没有针对确切 QQ 原生技术的书面授权；
- 实现贡献者对 NapCat 源码的接触范围没有限制；
- 只有通过禁止的复制、注入、修补、私有协议重建或未经授权的再分发才能实现所需行为；
- 通过启发式回退接受了不受支持的 QQ 构建；
- Direct 模式打开 TCP 监听器或回退到 OneBot；
- 一个绑定可以观察另一个绑定的账号、事件、秘密、状态或出站路由；
- 包生命周期依赖 `pip install latest` 或源码树回退。

## 首个切片的完成定义

- 所有准入门禁均已记录并获批；
- 不可变包通过 Package Spec v0.1 生命周期安装；
- 干净主机未预装 Connector 或 NapCat 材料；
- 能发现获授权的官方 QQ 运行时，并通过版本门禁；
- 一个绑定成功使用已有会话或 QR 登录；
- 私聊 / 群聊文本接收和发送均通过；
- 双绑定隔离通过；
- 重启后保留绑定并更改代次；
- stdio 是默认 IPC，Connector 自有 TCP 监听器仍为零；
- P2.5 衍生 CI 门禁和受保护的官方运行时冒烟测试通过；
- 清理证明不存在孤儿进程、IPC 端点、端口、容器或临时包 / 账号状态；
- 洁净室来源审查已签署通过。
