# Cyrene Multi-Repository Workspace Manual IDE Acceptance Guide

This document defines the manual verification procedure to validate that the Cyrene multi-repository developer environment correctly loads in **IntelliJ IDEA Ultimate** and **JetBrains Rider**.

---

## 0. Development Host vs. Runtime Target Principle

> [!IMPORTANT]
> **Windows is a supported DEVELOPMENT HOST**.
> Windows enables fast IDE indexing, rust-analyzer completion, test discovery, and local Python/.NET execution.
> **Windows is NOT the canonical acceptance or release platform for Rust kernel/runtime components**.
> Canonical Rust runtime validation is performed under Linux (`x86_64-unknown-linux-gnu`) via CI and `verify-linux.sh`.

---

## 1. Automated Acceptance Gate

Before performing manual IDE acceptance, ensure the automated verification gate passes:

```powershell
cd Cyrene-Workspace
.\bootstrap.ps1
.\verify.ps1
```

All automated checks must report `[PASS]`.

---

## 2. IntelliJ IDEA Multi-Project Workspace Acceptance

### Step 1: Open Workspace
1. Launch **IntelliJ IDEA Ultimate** (2024.3+ / 2026.x).
2. Select **File $\rightarrow$ Open...**
3. Select the folder: `Cyrene-Workspace`.
4. Select **Trust Project**.

### Step 2: Verify Multi-Project Workspace (`.idea/jb-workspace.xml`)
1. **Multi-Project Hierarchy & Active State**:
   - In the Project view, verify each repository appears as an independent native project module:
     - `Cyrene-Platform` (Cargo workspace, Python SDKs)
     - `Cyrene-Plugins-Official` (capability contracts, Python packages, Rust/.NET runtimes, Java SDKs, and TCKs)
     - `Cyrene-Services/Cyrene-Reactor` (Python Product runtime, Rust host-placement adapter)
     - `Cyrene-Services/Cyrene-Yield` (training lifecycle Product and direct Plugin adapters)
     - `Cyrene-Services/Cyrene-Exchange` (Python transport and Product gateway)
     - `Cyrene-Services/Cyrene-Catalyst`, `Cyrene-Echo`, `Cyrene-Navigator`
   - *Note*: Workspace projects may initially appear inactive in IntelliJ IDEA. To activate an unloaded project: **Right-click project $\rightarrow$ Load '<project>'**.
2. **VCS Multi-Root Registration**:
   - Open **Git** tool window (`Alt + 9`).
   - Verify all 8 repositories are registered as distinct Git roots.
3. **Gradle Projects & JDK 25**:
   - Open **Gradle** tool window.
   - Verify the live Plugins Gradle roots appear: `contracts/jvm` and `contracts/tck/model-provider-v1/jvm`.
   - Set the Gradle JVM to the registered **Temurin 25.0.4** SDK. These projects declare Kotlin 2.4.10 and `jvmToolchain(25)`; they do not depend on a retired Exchange coordinator or Spring gateway path.
4. **Rust Development Toolchain**:
   - Verify Cargo workspaces attach for `Cyrene-Platform` and `Cyrene-Services/Cyrene-Reactor`.
   - Open a Rust source file (e.g. `Cyrene-Platform/kernel/crates/cy-kernel-daemon/src/main.rs`).
   - Verify trait/struct navigation and code completion work natively via rust-analyzer.
5. **Python Interpreters (.venv)**:
   - Verify Python repositories bind to their available repo-local `.venv`/uv environments (Python 3.12 where `.python-version` is declared). Plugins is a component collection; its package and TCK `pyproject.toml` files are independent entrypoints and it has no root Python environment.
   - Open `Cyrene-Services/Cyrene-Yield/training/core/src/cy_exec/training/artifacts.py` $\rightarrow$ press `Ctrl + B` on `ArtifactKind` to verify symbol navigation into `cy_artifacts`.

### Step 3: Session Persistence
1. Close IntelliJ IDEA completely (`File $\rightarrow$ Exit`).
2. Re-open IntelliJ IDEA to `Cyrene-Workspace`.
3. Verify all VCS roots, Gradle projects, Cargo projects, and Python interpreters remain attached without manual configuration.

---

## 3. JetBrains Rider .NET Solution Acceptance

### Step 1: Open Solution
1. Launch **JetBrains Rider** (2024.3+).
2. Select **File $\rightarrow$ Open...**
3. Select `Cyrene-Workspace/Cyrene.Workspace.slnx`.

### Step 2: Verify Solution Hierarchy & Semantic Indexing
1. **Solution Structure**:
   - Verify the solution folders `/Plugins/Contracts/` and `/Plugins/Runtime/` are present.
   - Total: **12 projects** (4 contract/TCK projects and 8 Native AOT/runtime projects).
2. **Cross-Project Semantic Navigation**:
   - Verify C# semantic search finds symbols across projects.
   - Verify **Find Usages** (`Alt + F7`) and **Go to Implementation** work across solution projects.
3. **Unit Test Discovery**:
   - Open **Unit Tests** tool window (`Alt + 8`).
   - Verify tests from the Plugins solution projects are discovered.
4. **Aggregate Build**:
   - Select **Build $\rightarrow$ Build Solution** (`Ctrl + Shift + B`).
   - Verify build finishes with **0 warnings and 0 errors** experiments.

---

## 4. Acceptance Checklist

| Check Item | Target Tool | Expected Result | Verified |
|---|---|---|:---:|
| Automated Gate | PowerShell | `.\verify.ps1` reports all Passed | [ ] |
| Multi-Project Model | IntelliJ IDEA | `jb-workspace.xml` loads 8 independent projects | [ ] |
| VCS Multi-Root | IntelliJ IDEA | All 8 Git repositories recognized | [ ] |
| Gradle Baseline | IntelliJ IDEA | Gradle 9.6.0 + Kotlin 2.4.10 + JDK 25 resolve | [ ] |
| Cargo Attach | IntelliJ IDEA / RustRover | Platform & Reactor Cargo workspaces recognized | [ ] |
| Python Environment Binding | IntelliJ IDEA | Current Python repos use their declared uv/.venv environments; Plugins uses package entrypoints | [ ] |
| Python Navigation | IntelliJ IDEA | Cross-package symbols (`cy_artifacts`, etc.) resolve | [ ] |
| .NET Project Tree | JetBrains Rider | All 12 current Plugins projects load in `Cyrene.Workspace.slnx` | [ ] |
| .NET Semantic Search | JetBrains Rider | Symbol search & Find Usages work across projects | [ ] |
| .NET Test Explorer | JetBrains Rider | Plugins solution tests are discovered | [ ] |
| .NET Aggregate Build | JetBrains Rider | Clean build of all 12 current Plugins projects with 0 errors | [ ] |
| IDE Re-open Persistence | IDEA & Rider | Zero re-configuration required upon reopening | [ ] |

---

## 5. Parallel Agent Worktree & Semantic Endpoint Architecture

### 5.1 JetBrains Semantic Endpoint MCP Limitation
When working across parallel agent worktrees under `C:\cwt\<role>`, opening a standalone `.idea` project inside a secondary worktree window **does not automatically rebind or transfer existing Agent MCP sessions**. The active Agent MCP endpoint remains bound to the initial host process / window from which the ACP / MCP connection was established.

### 5.2 Safe Operating Patterns

To maintain 100% semantic authority and avoid stale or crossed symbol lookups across parallel worktrees:

#### Pattern A: Launch Agent Session Directly from Target Window (Recommended for Interactive Debugging)
- Open the dedicated worktree project window in JetBrains (e.g. `C:\cwt\my-feature-branch`).
- Start the AI Agent session directly within that window's terminal or ACP plugin interface.
- The Agent inherits the local project context, local Python `.venv`, and exact-worktree symbol index.

#### Pattern B: Hybrid Exact-Worktree Mode (Recommended for CLI / CI / Headless Orchestration)
- Maintain the primary workspace or main editor window for overall orchestration.
- Use `agent-task.ps1 start -Repo <repo> -Branch <branch> -Role <role>` to create an isolated worktree under `C:\cwt`.
- Worktrees are configured with worktree-local `.git/info/exclude` ignoring `.jspace`, `.idea`, and `.state`.
- Task tracking is stored in `C:\cwt\.state\<repo>\<role>\WORKSPACE.md`.
- Read-only consumers utilize detached exact-SHA snapshots via `agent-worktree.ps1 snapshot -Repo <repo> -Sha <sha> -Role <role>`.
- File edits, tests, and Git commits execute strictly against the dedicated worktree path (`C:\cwt\<role>`).
- On task completion, run `agent-task.ps1 stop -Role <role>` for clean fail-closed teardown.
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene 多仓库工作区 IDE 手动验收指南

本文定义手动验证流程，用于确认 Cyrene 多仓库开发环境能否在 **IntelliJ IDEA Ultimate** 和 **JetBrains Rider** 中正确加载。

---

## 0. 开发主机与运行时目标原则

> [!IMPORTANT]
> **Windows 是受支持的开发主机（DEVELOPMENT HOST）**。
> Windows 可加快 IDE 索引、rust-analyzer 补全、测试发现和本地 Python/.NET 执行。
> **Windows 不是 Rust 内核 / 运行时组件的规范验收或发布平台**。
> Rust 运行时的规范验证通过 CI 和 `verify-linux.sh` 在 Linux（`x86_64-unknown-linux-gnu`）下执行。

---

## 1. 自动验收门禁

开始手动 IDE 验收前，先确认自动验证门禁通过：

```powershell
cd Cyrene-Workspace
.\bootstrap.ps1
.\verify.ps1
```

所有自动检查都必须报告 `[PASS]`。

---

## 2. IntelliJ IDEA 多项目工作区验收

### 步骤 1：打开工作区
1. 启动 **IntelliJ IDEA Ultimate**（2024.3+ / 2026.x）。
2. 选择 **File → Open...**。
3. 选择文件夹：`Cyrene-Workspace`。
4. 选择 **Trust Project**。

### 步骤 2：验证多项目工作区（`.idea/jb-workspace.xml`）
1. **多项目层级与活动状态**：
   - 在 Project 视图中确认每个仓库都作为独立原生项目模块出现：
     - `Cyrene-Platform`（Cargo 工作区、Python SDK）
     - `Cyrene-Plugins-Official`（能力合约、Python 包、Rust/.NET 运行时、Java SDK 和 TCK）
     - `Cyrene-Services/Cyrene-Reactor`（Python Product 运行时、Rust 主机放置适配器）
     - `Cyrene-Services/Cyrene-Yield`（训练生命周期 Product 和直接 Plugin 适配器）
     - `Cyrene-Services/Cyrene-Exchange`（Python 传输层和 Product 网关）
     - `Cyrene-Services/Cyrene-Catalyst`、`Cyrene-Echo`、`Cyrene-Navigator`
   - *注意*：工作区项目最初可能在 IntelliJ IDEA 中显示为非活动状态。要启用尚未加载的项目：右键点击项目 → **Load '<project>'**。
2. **VCS 多根注册**：
   - 打开 **Git** 工具窗口（`Alt + 9`）。
   - 确认 8 个仓库分别注册为独立 Git 根目录。
3. **Gradle 项目与 JDK 25**：
   - 打开 **Gradle** 工具窗口。
   - 确认 Plugins 当前使用的 Gradle 根目录已显示：`contracts/jvm` 和 `contracts/tck/model-provider-v1/jvm`。
   - 将 Gradle JVM 设置为已注册的 **Temurin 25.0.4** SDK。这些项目声明 Kotlin 2.4.10 和 `jvmToolchain(25)`；它们不依赖已退役的 Exchange 协调器或 Spring 网关路径。
4. **Rust 开发工具链**：
   - 确认 `Cyrene-Platform` 和 `Cyrene-Services/Cyrene-Reactor` 的 Cargo 工作区均已挂载。
   - 打开一个 Rust 源文件（例如 `Cyrene-Platform/kernel/crates/cy-kernel-daemon/src/main.rs`）。
   - 确认可通过 rust-analyzer 原生执行 trait / struct 跳转和代码补全。
5. **Python 解释器（`.venv`）**：
   - 确认 Python 仓库绑定到可用的仓库本地 `.venv` / uv 环境（声明了 `.python-version` 的仓库使用 Python 3.12）。Plugins 是组件集合；其包和 TCK 的 `pyproject.toml` 文件是独立入口，不存在根级 Python 环境。
   - 打开 `Cyrene-Services/Cyrene-Yield/training/core/src/cy_exec/training/artifacts.py`，在 `ArtifactKind` 上按 `Ctrl + B`，确认符号能跳转至 `cy_artifacts`。

### 步骤 3：会话持久性
1. 完全退出 IntelliJ IDEA（**File → Exit**）。
2. 重新打开 IntelliJ IDEA 并进入 `Cyrene-Workspace`。
3. 确认所有 VCS 根目录、Gradle 项目、Cargo 项目和 Python 解释器仍然已挂载，无需手动重新配置。

---

## 3. JetBrains Rider .NET 解决方案验收

### 步骤 1：打开解决方案
1. 启动 **JetBrains Rider**（2024.3+）。
2. 选择 **File → Open...**。
3. 选择 `Cyrene-Workspace/Cyrene.Workspace.slnx`。

### 步骤 2：验证解决方案层级与语义索引
1. **解决方案结构**：
   - 确认存在解决方案文件夹 `/Plugins/Contracts/` 和 `/Plugins/Runtime/`。
   - 项目总数应为 **12 个**（4 个合约 / TCK 项目和 8 个 Native AOT / 运行时项目）。
2. **跨项目语义跳转**：
   - 确认 C# 语义搜索能找到各项目中的符号。
   - 确认 **Find Usages**（`Alt + F7`）和 **Go to Implementation** 可跨解决方案项目使用。
3. **单元测试发现**：
   - 打开 **Unit Tests** 工具窗口（`Alt + 8`）。
   - 确认能发现 Plugins 解决方案项目中的测试。
4. **聚合构建**：
   - 选择 **Build → Build Solution**（`Ctrl + Shift + B`）。
   - 确认构建完成，且 **0 个警告、0 个错误**。

---

## 4. 验收清单

| 检查项 | 目标工具 | 预期结果 | 已验证 |
|---|---|---|:---:|
| 自动门禁 | PowerShell | `.\verify.ps1` 报告全部通过 | [ ] |
| 多项目模型 | IntelliJ IDEA | `jb-workspace.xml` 加载 8 个独立项目 | [ ] |
| VCS 多根目录 | IntelliJ IDEA | 识别全部 8 个 Git 仓库 | [ ] |
| Gradle 基线 | IntelliJ IDEA | 正确解析 Gradle 9.6.0 + Kotlin 2.4.10 + JDK 25 | [ ] |
| Cargo 挂载 | IntelliJ IDEA / RustRover | 识别 Platform 与 Reactor Cargo 工作区 | [ ] |
| Python 环境绑定 | IntelliJ IDEA | 当前 Python 仓库使用声明的 uv/.venv 环境；Plugins 使用包入口 | [ ] |
| Python 跳转 | IntelliJ IDEA | 能解析跨包符号（`cy_artifacts` 等）| [ ] |
| .NET 项目树 | JetBrains Rider | 在 `Cyrene.Workspace.slnx` 中加载当前全部 12 个 Plugins 项目 | [ ] |
| .NET 语义搜索 | JetBrains Rider | 跨项目符号搜索和 Find Usages 可用 | [ ] |
| .NET 测试资源管理器 | JetBrains Rider | 能发现 Plugins 解决方案测试 | [ ] |
| .NET 聚合构建 | JetBrains Rider | 当前全部 12 个 Plugins 项目构建干净且无错误 | [ ] |
| IDE 重新打开后的持久性 | IDEA 与 Rider | 重新打开后无需重新配置 | [ ] |

---

## 5. 并行 Agent 工作树与语义端点架构

### 5.1 JetBrains 语义端点 MCP 限制
在 `C:\cwt\<role>` 下使用并行 Agent 工作树时，在次级工作树窗口中打开独立 `.idea` 项目，**不会自动重新绑定或转移现有 Agent MCP 会话**。活动 Agent MCP 端点仍绑定到最初建立 ACP / MCP 连接的主机进程 / 窗口。

### 5.2 安全操作模式

为保持完整的语义权威，并避免并行工作树之间的符号查询过期或串线，请使用以下模式：

#### 模式 A：直接从目标窗口启动 Agent 会话（交互式调试推荐）
- 在 JetBrains 中打开专用工作树项目窗口（例如 `C:\cwt\my-feature-branch`）。
- 直接在该窗口的终端或 ACP 插件界面中启动 AI Agent 会话。
- Agent 会继承本地项目上下文、本地 Python `.venv` 和精确工作树的符号索引。

#### 模式 B：精确工作树混合模式（CLI / CI / 无头编排推荐）
- 保留主工作区或主编辑器窗口，用于总体编排。
- 使用 `agent-task.ps1 start -Repo <repo> -Branch <branch> -Role <role>` 在 `C:\cwt` 下创建隔离工作树。
- 工作树通过工作树本地 `.git/info/exclude` 忽略 `.jspace`、`.idea` 和 `.state`。
- 任务跟踪记录在 `C:\cwt\.state\<repo>\<role>\WORKSPACE.md`。
- 只读消费者通过 `agent-worktree.ps1 snapshot -Repo <repo> -Sha <sha> -Role <role>` 使用脱离分支的精确 SHA 快照。
- 文件编辑、测试和 Git 提交都严格在专用工作树路径（`C:\cwt\<role>`）中执行。
- 任务完成后，运行 `agent-task.ps1 stop -Role <role>`，执行安全失败关闭式清理。
