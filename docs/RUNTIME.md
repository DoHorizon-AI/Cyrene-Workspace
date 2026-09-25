# Cyrene Runtime Execution Model

## 1. Clean-Room Runtime Architecture
Cyrene enforces a clean-room execution model to guarantee determinism, security, and reproducibility across development, staging, and production environments.

### Key Principles
1. **Zero Implicit Legacy Bindings**:
   - No historical shim loaders or deprecated compatibility wrappers are permitted in runtime paths.
   - All modules declare explicit contract versions.
2. **Deterministic Virtual Environments**:
   - Python dependencies are strictly locked using `uv` (`uv.lock`).
   - Rust dependencies are strictly pinned using `Cargo.lock`.
   - .NET dependencies are specified via pinned NuGet packages in `.slnx`.
3. **Daemon Socket Protocol**:
   - The platform daemon communicates with services via Unix Domain Sockets (Linux/macOS) or Named Pipes (Windows).
   - Standardized IPC framing ensures non-blocking streaming and backpressure handling.

## 2. Environment Profiles

| Profile | Target Environment | Security & Compatibility Posture |
| --- | --- | --- |
| `Development` | Local developer workstations | Developer tools enabled; verbose tracing; migration shims allowed only when explicitly configured |
| `Staging` | Pre-production validation | Clean-room configuration matching production; mock external APIs |
| `Production` | Production clusters & nodes | Compatibility fallbacks disabled; unauthorized legacy endpoints reject requests with HTTP 403 / exception |

## 3. Process Lifecycle
- **Initialization**:
  - Verification of runtime prerequisites (Python 3.12+, .NET 10, Rust 1.80+).
  - Validation of configuration schema against platform contracts.
- **Execution**:
  - Structured logging with OpenTelemetry tracing contexts.
  - Heartbeat reporting to the central daemon.
- **Graceful Shutdown**:
  - In-flight request draining with configurable timeout.
  - State flushing and ephemeral resource release.
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene 运行时执行模型

## 1. 洁净室运行时架构
Cyrene 强制采用洁净室执行模型，以保证开发、预发布和生产环境之间具有确定性、安全性和可复现性。

### 主要原则
1. **不进行隐式旧版绑定**：
   - 运行时路径中不得使用历史兼容垫片加载器或已弃用的兼容包装层。
   - 所有模块都必须声明明确的合约版本。
2. **确定性虚拟环境**：
   - Python 依赖通过 `uv`（`uv.lock`）严格锁定。
   - Rust 依赖通过 `Cargo.lock` 严格固定。
   - .NET 依赖通过 `.slnx` 中固定版本的 NuGet 包声明。
3. **守护进程 Socket 协议**：
   - Platform 守护进程通过 Unix Domain Socket（Linux/macOS）或命名管道（Windows）与服务通信。
   - 标准化 IPC 帧格式确保支持非阻塞流式传输和背压处理。

## 2. 环境配置档

| 配置档 | 目标环境 | 安全与兼容策略 |
| --- | --- | --- |
| `Development` | 本地开发者工作站 | 启用开发工具和详细追踪；仅在显式配置时允许迁移垫片 |
| `Staging` | 生产前验证 | 使用与生产匹配的洁净室配置；对外部 API 使用模拟服务 |
| `Production` | 生产集群与节点 | 禁用兼容回退；未授权的旧版端点通过 HTTP 403 / 异常拒绝请求 |

## 3. 进程生命周期
- **初始化**：
  - 验证运行时前置条件（Python 3.12+、.NET 10、Rust 1.80+）。
  - 根据 Platform 合约校验配置 schema。
- **执行**：
  - 使用 OpenTelemetry 追踪上下文进行结构化日志记录。
  - 向中央守护进程报告心跳。
- **平稳关闭**：
  - 在可配置的超时时间内排空正在处理的请求。
  - 刷新状态并释放临时资源。
