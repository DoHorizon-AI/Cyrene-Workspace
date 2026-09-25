# QQNT Direct Runtime Specification

## Status

Design-only specification. No native runtime implementation is authorized by
this document. Entry into implementation requires the gates in
`CLEAN_ROOM_BOUNDARY.md`.

## Identity model

| Identity | Meaning | Stable across restart | Sharing rule |
| --- | --- | --- | --- |
| Package artifact digest | Immutable connector code and locked dependencies | Yes | May be shared by many bindings. |
| Capability ID | What the connector can do: `message.connector.v1` | Yes | Shared by every compatible binding. |
| Binding ID | Which configured connector instance handles a request | Yes | Never shared as runtime state. |
| QQ account ID | Account selected after successful login | Yes, subject to operator configuration | Exactly one account per v0.1 binding. |
| Worker generation | One execution attempt of a binding | No | Changes after restart and is never an address. |
| Native session ID | One live official QQ session inside one generation | No | Owned by exactly one binding generation. |

The binding ID is the CES target. The QQ account ID is validated against the
binding's configured expectation after login but does not replace the binding
ID. A PID, process handle, native session, or worker generation must never be
persisted as configured identity.

## Package and profile model

- capability: `message.connector.v1`
- connector family: `qq`
- runtime profile: `qqnt-direct`
- compatibility profile: `onebot-v11`
- package: the same immutable official connector package lineage that currently
  carries the OneBot v11 profile

`qqnt-direct` maps authorized native QQ observations directly to the canonical
message contract. It does not serialize internal events as OneBot JSON and
parse them again. The `onebot-v11` profile remains a separately selected
compatibility transport within the package.

Changing the existing package ID is outside this design task. The implementation
change must version the current package and project the family/profile metadata
through the existing PluginManifest and Package Spec authority, without a new
registry or duplicate descriptor.

## Process architecture

```text
AstrBot or another Product
        |
        | Invoke / Subscribe(binding_id)
        v
Platform CapabilityExecutionService
        |
        | canonical worker protocol, inherited stdin/stdout
        v
QQ connector worker (one binding generation)
        |
        | inherited stdio
        | or owner-only UDS / named pipe if native isolation requires it
        v
Cyrene native helper (optional, one binding generation)
        |
        | authorized local integration boundary
        v
Official locally installed QQ runtime (one account/session)
```

The connector and helper open zero TCP listening ports. Outbound connections
made by the official QQ runtime remain owned by QQ and are not connector IPC.

## Host placement

The native helper and official QQ process must run on the same operating-system
side as the installed QQ runtime.

- Native Linux host: run worker/helper/QQ on Linux; a display/session provider
  may be required by the official package.
- Windows host: run native helper/QQ on Windows.
- Canonical desktop WSL host: Platform may remain in WSL. A Windows helper may
  be launched through WSL interoperability with inherited stdio and explicit
  process ownership. If reliable inherited stdio and process reaping cannot be
  proven, use an owner-restricted Windows named pipe bridged by a small
  Cyrene-owned launcher. Do not substitute localhost TCP.

Cross-boundary paths are configuration inputs resolved to canonical real paths.
Secrets and account data must stay on the native-runtime side and must not be
copied into the WSL package cache.

## Configuration model

The eventual schema must be versioned and binding-scoped. It should contain
only values or secret references needed for:

- `binding_id`;
- `runtime_profile = qqnt-direct`;
- expected QQ account identifier, optional before first QR login;
- official QQ installation discovery mode and optional explicit executable;
- binding-owned data directory;
- login policy: `existing_session` or `qr`;
- compatibility allow-list for QQ version/build and platform;
- restart budget and operation timeout bounds;
- secret references, never inline persistent secret values.

The OneBot endpoint/token fields are invalid when `runtime_profile` is
`qqnt-direct`. Explicit direct binding selection must never fall back to a
global OneBot endpoint.

## Runtime state machine

```text
CREATED
  -> DISCOVERING
  -> COMPATIBILITY_VERIFIED
  -> NATIVE_READY
  -> LOGIN_REQUIRED | LOGIN_IN_PROGRESS
  -> SESSION_STARTING
  -> READY
  -> DRAINING
  -> STOPPED
```

Any state may enter `FAILED`. A restart creates a new worker generation and a
new native session while preserving the binding ID. `READY` is reached only
after account identity is known, matches binding policy, the native session is
ready, and event publication is attached.

Unsupported QQ version, ambiguous installation discovery, account mismatch,
login rejection, helper protocol mismatch, and unavailable native runtime fail
closed. No request is queued for an arbitrary installation, account, or
binding.

## Native helper protocol

The helper protocol is a new Cyrene-owned, versioned, length-delimited protocol
over stdio. It carries:

- startup negotiation and supported operation versions;
- binding and generation correlation established by the parent;
- compatibility report for the discovered official runtime;
- login status and QR presentation payload or secure reference;
- session-ready/account-confirmed status;
- normalized private/group text receive events;
- text-send command and correlated result;
- cancellation, drain, and shutdown control;
- bounded diagnostic events without secrets or message contents by default.

The helper protocol does not expose CES directly and is never a Product API.
It contains no OneBot envelope. Correlation identifiers are unique within one
worker generation and cannot route across bindings.

## First-slice message mapping

### Receive

- private text -> canonical inbound message with private conversation scope;
- group text -> canonical inbound message with group conversation scope;
- content order preserved for the single supported text part;
- connector family, binding ID, self account, sender, conversation, message,
  timestamp, and worker generation are stamped from the active binding/session;
- duplicate suppression, if required, is keyed by binding plus native message
  identity, never by message ID alone.

### Send

- CES resolves an explicit or unambiguous binding before worker activation;
- the worker validates the request binding against its immutable startup
  binding;
- destination scope and text payload are validated;
- the command is sent only to that binding's helper/session;
- cancellation or deadline aborts correlation and returns the existing generic
  CES error model;
- result includes the binding-stamped external message reference when available.

Rich media, replies, mentions, proactive-send policy, and Product session policy
are outside the minimal implementation slice.

## Binding isolation requirements

For `qq-main` and `qq-secondary`:

- one worker process tree per active binding generation;
- one helper and one official QQ native session per binding;
- separate data directory, lock file, session state, secret handles, logs, and
  temporary media area;
- no shared mutable singleton, event bus, correlation table, login state, or
  outbound queue;
- immutable package/runtime files may be shared read-only by artifact digest;
- configuration is materialized separately for each activation;
- every inbound event is binding-stamped before it enters a shared Platform
  component;
- every outbound request is checked against the worker's startup binding;
- restart of one binding does not restart, drain, or mutate the other.

## Restart and shutdown

- unexpected process exit marks only that binding generation unavailable;
- restart uses a host-owned bounded backoff and creates a new generation;
- existing account state may be reused only through officially supported
  session behavior and only from that binding's data directory;
- repeated startup failures open a binding-local circuit and require operator
  action or an explicit retry window;
- graceful shutdown stops new invokes, cancels subscriptions, drains bounded
  in-flight sends, requests native session shutdown, closes IPC, and reaps the
  whole process tree;
- forced termination is bounded and followed by checks for child processes,
  IPC endpoints, file locks, and connector-owned listening ports.

## Security and observability

- secret values are resolved at activation and never written to package
  descriptors, logs, crash reports, or the package cache;
- QR data is short-lived, access-controlled, and redacted from normal logs;
- native helper and worker authenticate their inherited channel through process
  ownership plus a one-generation nonce;
- owner-only permissions are mandatory for UDS/named-pipe fallback;
- diagnostics identify package digest, binding ID, QQ build, state, and worker
  generation without exposing account credentials or message bodies;
- CI asserts zero connector-owned listening TCP sockets throughout startup,
  login simulation, send/receive, restart, and shutdown.

## Deterministic errors

- official QQ installation not found;
- multiple installations without an explicit selection;
- unsupported QQ version/build or native ABI;
- native interface not authorized/available;
- login required, rejected, expired, or account mismatch;
- binding unavailable or restarting;
- helper protocol incompatibility;
- send timeout or cancellation;
- native session closed;
- clean shutdown incomplete.

These map into existing generic Platform/CES status categories. No NapCat- or
Product-specific error is added to CES.
---
<!-- Chinese Translation / 中文翻译 -->

# QQNT Direct 运行时规范

## 状态

本文仅为设计规范，不授权任何原生运行时实现。开始实现前必须通过 `CLEAN_ROOM_BOUNDARY.md` 中规定的门禁。

## 身份模型

| 身份 | 含义 | 重启后是否稳定 | 共享规则 |
|---|---|---|---|
| 包制品摘要 | 不可变的 Connector 代码和已锁定依赖 | 是 | 可由多个绑定共享。|
| Capability ID | Connector 提供的能力：`message.connector.v1` | 是 | 所有兼容绑定共享。|
| Binding ID | 处理请求的已配置 Connector 实例 | 是 | 不得作为运行时状态共享。|
| QQ 账号 ID | 成功登录后选定的账号 | 是，受运营方配置约束 | v0.1 中每个绑定只能对应一个账号。|
| Worker generation | 某个绑定的一次执行尝试 | 否 | 重启后改变，绝不能作为地址。|
| Native session ID | 某一代运行时中的一个活动官方 QQ 会话 | 否 | 仅由一个绑定代次拥有。|

Binding ID 是 CES 的目标。登录后会根据绑定配置的预期值校验 QQ 账号 ID，但账号 ID 不会取代 Binding ID。PID、进程句柄、原生会话或 Worker 代次绝不能作为已配置身份持久化。

## 包与配置档模型

- capability：`message.connector.v1`
- connector family：`qq`
- runtime profile：`qqnt-direct`
- compatibility profile：`onebot-v11`
- package：当前承载 OneBot v11 配置档的同一条官方 Connector 不可变包血缘

`qqnt-direct` 将经过授权的原生 QQ 观测直接映射到规范消息合约。不将内部事件序列化为 OneBot JSON 后再重新解析。`onebot-v11` 配置档仍作为该包中单独选择的兼容传输方式。

修改现有包 ID 不属于本设计任务。实现变更必须对当前包进行版本化，并通过现有 PluginManifest 和 Package Spec 权威投影 family / profile 元数据；不得新建 registry 或重复描述符。

## 进程架构

```text
AstrBot 或其他 Product
        |
        | Invoke / Subscribe(binding_id)
        v
Platform CapabilityExecutionService
        |
        | 规范 worker 协议，通过继承的 stdin/stdout 通信
        v
QQ Connector worker（一个绑定代次）
        |
        | 继承的 stdio
        | 或在原生隔离有此要求时使用仅所有者可访问的 UDS / 命名管道
        v
Cyrene 原生 helper（可选，一个绑定代次）
        |
        | 经授权的本地集成边界
        v
官方本地安装的 QQ 运行时（一个账号 / 会话）
```

Connector 和 helper 不打开任何 TCP 监听端口。官方 QQ 运行时发起的出站连接仍由 QQ 自行拥有，并不属于 Connector IPC。

## 主机放置

原生 helper 和官方 QQ 进程必须运行在与已安装 QQ 运行时相同的一侧操作系统中。

- **原生 Linux 主机**：在 Linux 上运行 worker/helper/QQ；官方软件包可能要求提供显示 / 会话服务。
- **Windows 主机**：在 Windows 上运行原生 helper/QQ。
- **规范桌面 WSL 主机**：Platform 可以留在 WSL 中。可通过 WSL 互操作启动 Windows helper，使用继承的 stdio 并明确归属进程。如果无法证明继承的 stdio 和进程回收可靠，则使用由小型 Cyrene 自有启动器桥接、受所有者权限限制的 Windows 命名管道。不得用 localhost TCP 替代。

跨边界路径是配置输入，并须解析为规范真实路径。秘密和账号数据必须留在原生运行时所在的一侧，不得复制进 WSL 包缓存。

## 配置模型

最终 schema 必须有版本号并限定在单个绑定内。只应包含以下必需值或秘密引用：

- `binding_id`；
- `runtime_profile = qqnt-direct`；
- 预期 QQ 账号标识（首次 QR 登录前可选）；
- 官方 QQ 安装发现模式和可选的显式可执行文件路径；
- 绑定自有的数据目录；
- 登录策略：`existing_session` 或 `qr`；
- QQ 版本 / 构建及平台的兼容性允许列表；
- 重启预算和操作超时边界；
- 秘密引用；不得内嵌持久化秘密值。

当 `runtime_profile` 为 `qqnt-direct` 时，OneBot endpoint / token 字段无效。显式选择 direct binding 时，绝不能回退到全局 OneBot endpoint。

## 运行时状态机

```text
CREATED
  -> DISCOVERING
  -> COMPATIBILITY_VERIFIED
  -> NATIVE_READY
  -> LOGIN_REQUIRED | LOGIN_IN_PROGRESS
  -> SESSION_STARTING
  -> READY
  -> DRAINING
  -> STOPPED
```

任何状态都可以进入 `FAILED`。重启会创建新的 Worker 代次和原生会话，但保留 Binding ID。只有确认账号身份、符合绑定策略、原生会话就绪且事件发布已连接后，状态才能变为 `READY`。

QQ 版本不受支持、安装位置发现存在歧义、账号不匹配、登录遭拒、helper 协议不匹配或原生运行时不可用时，都必须快速失败。不得将请求排入任意安装、账号或绑定。

## 原生 helper 协议

helper 协议是 Cyrene 自有的新版本化、带长度界定的 stdio 协议，传递以下信息：

- 启动协商和所支持的操作版本；
- 由父进程建立的绑定与代次关联信息；
- 对发现的官方运行时所做的兼容性报告；
- 登录状态和 QR 展示载荷或安全引用；
- 会话就绪 / 账号已确认状态；
- 已规范化的私聊 / 群聊文本接收事件；
- 文本发送命令及其关联结果；
- 取消、排空和关闭控制；
- 默认不含秘密或消息正文、且有界的诊断事件。

helper 协议不直接暴露 CES，也绝不是 Product API。协议中没有 OneBot 信封。关联标识在一个 Worker 代次内唯一，不能跨绑定路由。

## 首个切片的消息映射

### 接收

- 私聊文本 → 作用域为私聊会话的规范入站消息；
- 群聊文本 → 作用域为群聊会话的规范入站消息；
- 保留唯一受支持文本片段中的内容顺序；
- Connector family、Binding ID、自身账号、发送者、会话、消息、时间戳和 Worker 代次均从活动绑定 / 会话写入；
- 若需要去重，应使用绑定与原生消息身份的组合作为键，绝不能只依赖消息 ID。

### 发送

- CES 在激活 Worker 前解析出显式或无歧义的绑定；
- Worker 根据其不可变启动绑定校验请求绑定；
- 校验目标作用域和文本载荷；
- 只向该绑定的 helper / 会话发送命令；
- 取消或达到截止时间时，中止关联并返回现有通用 CES 错误模型；
- 结果中尽可能包含带绑定标记的外部消息引用。

富媒体、回复、提及、主动发送策略和 Product 会话策略不属于最小实现切片。

## 绑定隔离要求

针对 `qq-main` 和 `qq-secondary`：

- 每个活动绑定代次拥有一个 Worker 进程树；
- 每个绑定拥有一个 helper 和一个官方 QQ 原生会话；
- 数据目录、锁文件、会话状态、秘密句柄、日志和临时媒体目录彼此隔离；
- 不共享可变单例、事件总线、关联表、登录状态或出站队列；
- 不可变包 / 运行时文件可按制品摘要只读共享；
- 每次激活时分别物化配置；
- 每个入站事件进入共享 Platform 组件之前都必须带上绑定标记；
- 每个出站请求都要依据 Worker 的启动绑定进行校验；
- 重启一个绑定不得重启、排空或修改另一个绑定。

## 重启与关闭

- 进程意外退出时，只将该绑定代次标记为不可用；
- 重启使用由主机控制、有界的退避策略，并创建新代次；
- 只有官方支持的会话行为允许复用已有账号状态时，才能从该绑定的数据目录中复用；
- 连续启动失败会打开绑定本地熔断器，需要运营方介入或显式重试窗口；
- 平稳关闭时停止接收新调用、取消订阅、在有界时间内排空进行中的发送、请求关闭原生会话、关闭 IPC，并回收整个进程树；
- 强制终止必须有时限；之后检查子进程、IPC 端点、文件锁和 Connector 自有监听端口。

## 安全与可观测性

- 激活时解析秘密值，绝不写入包描述符、日志、崩溃报告或包缓存；
- QR 数据有效期短、受访问控制，并从常规日志中脱敏；
- 原生 helper 和 Worker 通过进程所有权及单代次 nonce 验证继承的通信通道；
- 使用 UDS / 命名管道回退时必须应用仅所有者可访问的权限；
- 诊断信息记录包摘要、Binding ID、QQ 构建版本、状态和 Worker 代次，但不得暴露账号凭据或消息正文；
- CI 在启动、模拟登录、发送 / 接收、重启和关闭全过程中断言 Connector 自有 TCP 监听 Socket 数为零。

## 确定性错误

- 未找到官方 QQ 安装；
- 存在多个安装且未明确选择；
- QQ 版本 / 构建或原生 ABI 不受支持；
- 原生接口未获授权 / 不可用；
- 需要登录、登录遭拒、登录过期或账号不匹配；
- 绑定不可用或正在重启；
- helper 协议不兼容；
- 发送超时或被取消；
- 原生会话已关闭；
- 未完成干净关闭。

这些错误映射到现有通用 Platform/CES 状态类别。不得在 CES 中新增 NapCat 专属或 Product 专属错误。
