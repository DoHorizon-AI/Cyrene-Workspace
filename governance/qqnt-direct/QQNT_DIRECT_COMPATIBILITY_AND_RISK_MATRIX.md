# QQNT Direct Compatibility and Risk Matrix

## Compatibility matrix

| Dimension | Initial position | Evidence required | Failure behavior |
| --- | --- | --- | --- |
| Capability | `message.connector.v1@1` | Existing contract/TCK and CES binding tests | Reject incompatible Platform API. |
| Connector family | `qq` | Package descriptor and PluginManifest projection | Reject missing or conflicting family metadata. |
| Runtime profile | `qqnt-direct` | Explicit binding configuration | Never infer from an endpoint or first available profile. |
| Compatibility profile | `onebot-v11` remains available in the same package | Existing OneBot TCK and package lifecycle gate | Direct mode never falls back to OneBot. |
| Windows x64 QQ | Candidate first native placement, subject to approval | Exact official installer, QQ build, ABI, terms, and native-interface approval | Unknown build fails closed. |
| Linux x86_64 QQ | Discovery/design candidate only | Exact official deb/rpm/AppImage source, display/session needs, ABI, and terms approval | Unsupported until separately admitted. |
| Other Linux architectures | Not in first slice | Official package availability plus independent native compatibility evidence | Unsupported. |
| WSL Platform + Windows QQ | Preferred desktop split if stdio ownership is reliable | End-to-end process reaping, path canonicalization, secret placement, stdio behavior | Named pipe fallback or unsupported; no TCP fallback. |
| Native IPC | Inherited stdio | Framing, cancellation, backpressure, malformed input, process ownership tests | Fail activation if unavailable. |
| IPC fallback | Owner-only UDS or Windows named pipe | Permission and lifecycle tests | No localhost listener fallback. |
| Account model | One QQ account/session per binding process tree | Dual-binding isolation and restart tests | Account mismatch fails binding activation. |
| Package reuse | One immutable artifact/runtime shared read-only by digest | Package-installed vertical gate | Reject digest mismatch or source fallback. |
| Mutable state | Binding-private | Filesystem, secret, event, queue, and process isolation tests | Stop affected binding; never borrow peer state. |
| Login | Existing official session or QR | Authorized interface and dedicated-account smoke | No password/captcha bypass fallback. |
| Message scope | Private/group text | Independent mapping tests and official-runtime smoke | Unsupported content returns deterministic error. |
| Rich media | Deferred | Separate media lifecycle, dependency, and legal review | Explicitly unsupported in first slice. |

## Risk matrix

| Risk | Level | Current evidence | Required mitigation / decision | Owner gate |
| --- | --- | --- | --- | --- |
| NapCat license risk | Critical if reused; Low when excluded | Pinned repository uses a restrictive custom license with non-commercial and redistribution conditions. | Zero code/types/tests/hooks/schemas/binaries/runtime dependency; exposed researcher does not implement native wrapper; provenance review. | Legal + clean-room reviewer |
| QQ platform/terms risk | Critical | Tencent publishes QQ software terms, but this audit found no clear authorization for third-party native wrapping or automation. | Written approval for exact technique and test-account use; reject injection/reverse-engineering/redistribution unless specifically authorized. | Product owner + counsel |
| Technical version coupling | High | Observable implementations depend on QQ build, platform layout, architecture, and native compatibility. | Exact compatibility allow-list, discovery report, pinned test image, fail closed, rapid rollback. | Runtime maintainer |
| Windows/WSL process placement | High | QQ may live on Windows while Platform runs in WSL; process ownership, paths, signals, and secrets cross OS boundaries. | Inherited stdio proof, explicit Windows process-tree ownership, canonical paths, native-side account state; named pipe only if needed. | Platform/runtime maintainer |
| Native library distribution | Critical | Official QQ packages are downloadable, but redistribution rights for their libraries were not established. | Discover separately installed official QQ; never bundle libraries without written redistribution authority. | Release + legal |
| Clean-room provenance | High | Architecture author was exposed to high-level source structure during restricted research. | Separate implementation owner, input log, independent naming/design, diff review, no reference checkout in implementation/CI. | Clean-room reviewer |
| Undocumented native interface | Critical | No Tencent-published stable desktop automation/native event API was established. | Obtain authorized interface or stop. Do not substitute private hooks/protocol extraction. | Architecture + legal |
| Account restriction/risk control | High | Official upstream security guidance warns of account restrictions and disconnect behavior. | Dedicated accounts, isolated environment, bounded traffic, kill switch, no circumvention. | Operations + product owner |
| Secret/session leakage | High | Two bindings share package code but must not share mutable account state. | Binding-private directories and secret handles; one process tree/session per binding; adversarial overlap tests. | Security + runtime maintainer |
| Cross-instance routing | High | Shared capability and implementation can coexist under multiple bindings. | Binding stamped at ingress, immutable worker startup binding, correlation scoped by binding/generation, CES explicit selection. | Platform + connector maintainer |
| Hidden OneBot fallback | High | Same package contains a OneBot compatibility profile. | Mutually exclusive config branches; direct mode rejects endpoint fields; test with no OneBot endpoint and source unavailable. | Connector maintainer |
| Listening-port regression | Medium/High | Network adapters are common in compatibility runtimes. | Socket audit for startup/login/send/restart/shutdown; default profile permits only stdio or local owner-only IPC. | CI owner |
| Native crash/orphan process | High | Native runtimes and cross-OS helpers may not terminate with their parent automatically. | Process-group/job ownership, bounded graceful shutdown, forced reap, post-test orphan/IPC/lock checks. | Runtime maintainer |
| Message/media data handling | Medium for text; High for media | Text mapping is bounded; media adds upload/download/temp lifecycle and native dependencies. | Text-only first slice; separate media design with retention and attachment isolation. | Connector + security |
| Package supply chain | High | Native helper is privileged local code and package lifecycle is dynamic. | Immutable artifact, exact lock, digest, optional signature/SBOM, verified cache, no install-latest, upgrade/rollback gate. | Release + security |

## Risk decisions

### NapCat license

Risk is controlled only by exclusion. NapCat is a research reference, not a
component, dependency, test fixture, or compatibility runtime. Any request to
reuse its implementation reopens legal review and invalidates the current
clean-room plan.

### QQ platform and terms

This is the principal blocker. Public availability of a QQ installer does not
imply permission to wrap internal APIs, automate a user account, modify the
process, or redistribute native libraries. Implementation readiness remains
`NO` until the exact boundary is authorized.

### Version coupling

The runtime is admitted per exact QQ build/platform/architecture tuple. A
package version records the supported tuple and native-helper ABI. Automatic QQ
updates must move a binding to `UNAVAILABLE_VERSION` until compatibility is
proven; they must not trigger an arbitrary best-effort load.

### Windows/WSL placement

The preferred desktop arrangement keeps CES and the generic worker authority in
WSL while placing only the native helper/session beside Windows QQ. Inherited
stdio is the first transport. Windows named pipe is the bounded fallback. If
neither provides reliable ownership and cleanup, the profile is unsupported in
that topology.

### Native distribution constraints

The Cyrene package distributes only Cyrene-owned code and locked open-source
dependencies with compatible licenses. The official QQ runtime is installed
separately from Tencent's official channel. QQ libraries, assets, account data,
and update payloads are not copied into the package or offline cache.

### Clean-room provenance

Research and implementation roles are separated. The implementation record
must show independent origin for native APIs, protocol framing, state machine,
tests, and constants. Unexplained structural similarity is a release blocker.

## Current decision summary

- NapCat code reuse: **NO**.
- NapCat runtime dependency: **NO**.
- Portless `qqnt-direct` architecture: **FROZEN**.
- Package/profile design: **FROZEN**, subject to ordinary manifest/schema review.
- Clean-room native implementation: **NOT READY** until QQ terms and an
  authorized native interface are recorded.
---
<!-- Chinese Translation / 中文翻译 -->

# QQNT Direct 兼容性与风险矩阵

## 兼容性矩阵

| 维度 | 初始立场 | 所需证据 | 失败行为 |
|---|---|---|---|
| Capability | `message.connector.v1@1` | 现有合约 / TCK 和 CES binding 测试 | 拒绝不兼容的 Platform API。|
| Connector family | `qq` | 包描述符及 PluginManifest 投影 | family 元数据缺失或冲突时拒绝。|
| Runtime profile | `qqnt-direct` | 显式绑定配置 | 绝不根据 endpoint 或最先可用的配置档推断。|
| Compatibility profile | 同一个包内继续提供 `onebot-v11` | 现有 OneBot TCK 和包生命周期门禁 | Direct 模式绝不回退到 OneBot。|
| Windows x64 QQ | 首个候选原生放置位置，须经批准 | 确切的官方安装程序、QQ 构建版本、ABI、条款及原生接口批准 | 未知构建版本必须快速失败。|
| Linux x86_64 QQ | 仅作为发现 / 设计候选 | 确切的官方 deb/rpm/AppImage 来源、显示 / 会话需求、ABI 和条款批准 | 单独准入前不支持。|
| 其他 Linux 架构 | 不在首个切片内 | 官方软件包可用性及独立原生兼容性证据 | 不支持。|
| WSL Platform + Windows QQ | 如果 stdio 进程归属可靠，这是首选桌面拆分方式 | 端到端进程回收、路径规范化、秘密放置和 stdio 行为 | 使用命名管道回退或标记为不支持；不能回退到 TCP。|
| 原生 IPC | 继承的 stdio | 帧格式、取消、背压、畸形输入和进程归属测试 | 不可用时拒绝激活。|
| IPC 回退 | 仅所有者可访问的 UDS 或 Windows 命名管道 | 权限和生命周期测试 | 不得回退到 localhost 监听端口。|
| 账号模型 | 每个绑定进程树对应一个 QQ 账号 / 会话 | 双绑定隔离和重启测试 | 账号不匹配时拒绝激活绑定。|
| 包复用 | 多个绑定按摘要只读共享一个不可变制品 / 运行时 | 安装包后的垂直门禁 | 摘要不匹配或回退到源码时拒绝。|
| 可变状态 | 绑定私有 | 文件系统、秘密、事件、队列和进程隔离测试 | 停止受影响的绑定；不得借用其他绑定状态。|
| 登录 | 现有官方会话或 QR | 授权接口和专用账号冒烟测试 | 不得回退到密码 / 验证码绕过方案。|
| 消息范围 | 私聊 / 群聊文本 | 独立映射测试和官方运行时冒烟测试 | 对不支持的内容返回确定性错误。|
| 富媒体 | 延后处理 | 单独的媒体生命周期、依赖和法律审查 | 首个切片明确不支持。|

## 风险矩阵

| 风险 | 等级 | 当前证据 | 必需的缓解措施 / 决策 | 责任门禁 |
|---|---|---|---|---|
| NapCat 许可风险 | 复用时为 Critical；排除后为 Low | 固定引用的仓库使用限制严格的自定义许可，包含非商业和再分发条件。| 不得使用任何代码 / 类型 / 测试 / Hook / schema / 二进制文件 / 运行时依赖；接触过研究材料的人不得实现原生包装层；执行来源审查。| 法务 + 洁净室审查者 |
| QQ 平台 / 条款风险 | Critical | 腾讯发布了 QQ 软件条款，但本次审计没有找到第三方原生包装或自动化获得明确授权的证据。| 针对确切技术和测试账号用途取得书面批准；除非获得明确授权，否则拒绝注入 / 逆向工程 / 再分发。| Product 负责人 + 法务顾问 |
| 技术版本耦合 | High | 可观察到的实现依赖 QQ 构建版本、平台布局、架构和原生兼容性。| 使用精确兼容性允许列表、发现报告、固定的测试镜像；快速失败并支持快速回滚。| 运行时维护者 |
| Windows/WSL 进程放置 | High | QQ 可能在 Windows 上运行，而 Platform 位于 WSL；进程所有权、路径、信号和秘密需要跨操作系统边界。| 证明继承 stdio 可用，明确 Windows 进程树归属，使用规范路径并将账号状态留在原生侧；确有需要时才使用命名管道。| Platform / 运行时维护者 |
| 原生库再分发 | Critical | 官方 QQ 软件包可下载，但尚未确认其库的再分发权。| 发现单独安装的官方 QQ；没有书面再分发授权时绝不捆绑库。| 发布负责人 + 法务 |
| 洁净室来源 | High | 架构文档作者在受限研究中接触过高层源码结构。| 分离实现负责人，记录输入，独立设计命名，审查差异；实现和 CI 中不提供参考源码检出。| 洁净室审查者 |
| 未公开的原生接口 | Critical | 尚未确认腾讯发布了稳定的桌面自动化 / 原生事件 API。| 获取授权接口，否则停止。不得以私有 Hook / 协议提取替代。| 架构 + 法务 |
| 账号限制 / 风控 | High | 官方上游安全指引提示存在账号限制和断连行为。| 使用专用账号和隔离环境，限制流量并提供 kill switch；不得规避限制。| 运维 + Product 负责人 |
| 秘密 / 会话泄漏 | High | 两个绑定可以共享包代码，但不能共享可变账号状态。| 绑定私有目录和秘密句柄；每个绑定使用单独的进程树 / 会话；执行对抗性并发测试。| 安全 + 运行时维护者 |
| 跨实例路由 | High | 多个绑定可以共用能力及实现。| 入站时标注绑定；使用不可变 Worker 启动绑定；关联作用域限定到绑定 / 代次；由 CES 显式选择。| Platform + Connector 维护者 |
| 隐式 OneBot 回退 | High | 同一个包中包含 OneBot 兼容配置档。| 使用互斥的配置分支；Direct 模式拒绝 endpoint 字段；在无 OneBot endpoint 且源码不可用时进行测试。| Connector 维护者 |
| 监听端口回归 | Medium/High | 兼容运行时中常见网络适配器。| 在启动 / 登录 / 发送 / 重启 / 关闭阶段审计 Socket；默认配置档只允许 stdio 或仅所有者可访问的本地 IPC。| CI 负责人 |
| 原生崩溃 / 孤儿进程 | High | 原生运行时和跨操作系统 helper 可能不会随父进程自动终止。| 使用进程组 / Job 归属、有界的平稳关闭和强制回收；测试后检查孤儿进程 / IPC / 锁。| 运行时维护者 |
| 消息 / 媒体数据处理 | 文本为 Medium；媒体为 High | 文本映射范围有限；媒体会增加上传 / 下载 / 临时文件生命周期和原生依赖。| 首个切片仅支持文本；媒体需单独设计保留策略和附件隔离。| Connector + 安全 |
| 包供应链 | High | 原生 helper 是具有本地权限的代码，包生命周期也会动态变化。| 使用不可变制品、精确锁文件、摘要、可选签名 / SBOM、经过验证的缓存；禁止安装 latest；设置升级 / 回滚门禁。| 发布 + 安全 |

## 风险决策

### NapCat 许可

只有彻底排除 NapCat，才能控制该风险。NapCat 是研究参考资料，不是组件、依赖、测试夹具或兼容运行时。任何复用其实现的请求都会重新触发法律审查，并使当前洁净室计划失效。

### QQ 平台与条款

这是首要阻塞项。可以公开下载 QQ 安装程序，并不代表获准包装其内部 API、自动操作用户账号、修改进程或再分发原生库。在确切边界获得授权之前，实现准备状态仍为 `NO`。

### 版本耦合

运行时按精确的 QQ 构建版本 / 平台 / 架构组合逐项准入。包版本需记录支持的组合和原生 helper ABI。QQ 自动更新后，绑定必须转为 `UNAVAILABLE_VERSION`，直到兼容性得到证明；不得尝试任意的尽力加载。

### Windows/WSL 放置

首选桌面方案是让 CES 和通用 Worker 权威留在 WSL，只把原生 helper / 会话放在 Windows QQ 旁边。首选传输方式是继承的 stdio；有界回退方式是 Windows 命名管道。如果两者都无法保证可靠归属和清理，则此拓扑不支持该配置档。

### 原生分发约束

Cyrene 包只分发 Cyrene 自有代码和具有兼容许可的已锁定开源依赖。官方 QQ 运行时必须从腾讯官方渠道单独安装。不得将 QQ 库、资源、账号数据或更新载荷复制到软件包或离线缓存中。

### 洁净室来源

研究和实现角色彼此分离。实现记录必须证明原生 API、协议帧格式、状态机、测试和常量均为独立创作。无法解释的结构相似性属于发布阻塞项。

## 当前决策摘要

- 复用 NapCat 代码：**否**。
- 依赖 NapCat 运行时：**否**。
- 无端口 `qqnt-direct` 架构：**已冻结**。
- 包 / 配置档设计：**已冻结**，仍需遵循常规 manifest/schema 审查。
- 洁净室原生实现：在记录 QQ 条款和获授权原生接口之前，**尚未就绪**。
