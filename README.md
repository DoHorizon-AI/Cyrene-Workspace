# Cyrene Developer Meta-Workspace

This meta-repository provides a unified, reproducible developer workspace configuration for all Cyrene repositories.

---

## 1. Architecture & Policy

- **Topology Authority**: [`repositories.yaml`](repositories.yaml) records all canonical Cyrene repositories, relative paths, remotes, and build systems.
- **CI Authority**: [`docs/governance/ci-authority.md`](docs/governance/ci-authority.md) records the single automatic source gate and Azure supplemental scopes for every repository.
- **Zero Absolute Paths**: All workspace, solution, and IDE configurations use relative paths.
- **Independence Guarantee**: Each repository remains fully buildable and testable on its own without requiring `Cyrene-Workspace`.
- **Language Baselines**:
  - **.NET**: .NET 10 LTS (`global.json`, `Cyrene.Workspace.slnx`)
  - **Python**: `>=3.11` (`uv`, `pyproject.toml`, `.python-version = 3.12`, repo-local `.venv`)
  - **Rust**: Cargo workspaces (`Cyrene-Platform/Cargo.toml`, `Cyrene-Services/Cyrene-Reactor/Cargo.toml`)
  - **JVM / Kotlin**: Gradle Kotlin DSL with Java Toolchains (Java 21/25, Kotlin 2.0+)

### Cross-project API naming constitution

All Cyrene repositories share the canonical API vocabulary owned by
Cyrene-Platform. Before adding or renaming a cross-repository API, read the
[Cyrene API Naming Constitution](https://github.com/DoHorizon-AI/Cyrene-Platform/blob/develop/docs/governance/API_NAMING_CONSTITUTION.md).
It is mandatory to preserve the semantic distinctions between
`Acquire`/`Reserve`, `Start`/`Launch`, `Stop`/`Terminate`,
`Watch`/`Subscribe`, and `State`/`Status`/`Phase` across Yield, Reactor,
Exchange, Plugins, and the other Product repositories.

Platform is the language source for shared terms. A repository must not add a
local synonym for an existing Platform concept. Any deliberate domain-specific
distinction must be documented in that repository and must not create a second
authority. Breaking renames are permitted before the first public release; the
Cyrene public API naming freeze begins with that release.

所有 Cyrene 仓库共享由 Cyrene-Platform 维护的 API 统一词汇。跨仓库新增或
重命名 API 前必须阅读上面的命名宪法；Yield、Reactor、Exchange、Plugins
及其他 Product 仓库不得重新发明已有 Platform 术语或用本地同义词替代。

### Canonical Repository Inventory

| Repository | Relative Path | Role & Technology |
| :--- | :--- | :--- |
| `Cyrene-Platform` | `../Cyrene-Platform` | Core platform kernel, native sys/nvidia runtime adapters, execution engine, and Python SDK (Pure Rust + Python baseline; decoupled from product control-planes per PR #41) |
| `Cyrene-Plugins-Official` | `../Cyrene-Plugins-Official` | Public-target capability contracts, SDKs, TCKs, and replaceable implementations |
| `Cyrene-Reactor` | `../Cyrene-Services/Cyrene-Reactor` | Inference deployment/endpoint Product and direct execution-engine consumer |
| `Cyrene-Yield` | `../Cyrene-Services/Cyrene-Yield` | Distributed training orchestration and sole authority for `ModelVersion` |
| `Cyrene-Exchange` | `../Cyrene-Services/Cyrene-Exchange` | Service exchange gateway, dispatch contracts, and backend coordinator |
| `Cyrene-Catalyst` | `../Cyrene-Services/Cyrene-Catalyst` | Dataset curation and sole authority for `DatasetVersion` |
| `Cyrene-Echo` | `../Cyrene-Services/Cyrene-Echo` | Post-inference evaluation and sole authority for `EvaluationRun` / `HumanAnnotation` / `FeedbackSet` |
| `Cyrene-Navigator` | `../Cyrene-Services/Cyrene-Navigator` | Desktop client experience and sole authority for `Conversation` / `AgentRun` sessions |
| `Cyrene-Client` | `../Cyrene-Client` | React/TypeScript/Vite pipeline workbench and operator console surface |

> [!NOTE]
> **Repository vs. Subcomponent Boundary**  
> Only the 9 canonical repositories above are top-level Git repositories in the Cyrene workspace.
> Current Gradle projects belong to the Plugins repository under `contracts/jvm` and `contracts/tck/model-provider-v1/jvm`; they are linked as build roots, not as standalone repositories. The retired Exchange coordinator, legacy Spring gateway, and Platform JVM control-plane paths are historical references only and must not be reintroduced into `repositories.yaml` or `.idea/jb-workspace.xml`.
>
> `Cyrene-Client` is currently private. Authenticate the GitHub CLI with organization access (`gh auth login`) before running the full PowerShell bootstrap on a machine where Client has not already been cloned.

---

## 2. Quick Start

### Windows (PowerShell)

```powershell
cd Cyrene-Workspace
.\bootstrap.ps1
.\verify.ps1
```

### Linux / macOS (Bash)

```bash
cd Cyrene-Workspace
./bootstrap.sh
```

---

## 3. Opening in IDEs

- **IntelliJ IDEA Ultimate / WebStorm / PyCharm / CLion**:
  Open the `Cyrene-Workspace` folder. The workspace automatically mounts the 9 canonical repositories via `.idea/jb-workspace.xml`.
- **JetBrains Rider / Visual Studio**:
  Open `Cyrene-Workspace/Cyrene.Workspace.slnx`.

See [`IDE_ACCEPTANCE.md`](IDE_ACCEPTANCE.md) for the manual acceptance checklist.

---

## 4. Parallel Agent Worktree Isolation

> **Canonical Rule**:  
> Agents must never use another Agent's mutable working tree as an integration baseline. Exchange exact remote SHAs and use detached snapshots.

### Usage

Use canonical repository names (or clean 1:1 convenience aliases like `platform`, `plugins`, `reactor`, `yield`, `exchange`, `catalyst`, `echo`, `navigator`, `studio`):

```powershell
# Create dedicated task worktree for writer agent
.\agent-worktree.ps1 create -Repo Cyrene-Plugins-Official -Branch chore/spring-boot-4-1-1 -Base origin/develop -Role idea-spring

# Create detached immutable snapshot worktree for consumer agent
.\agent-worktree.ps1 snapshot -Repo Cyrene-Platform -Sha <40-char-sha> -Role rider-media-platform

# View active agent worktree ownership
.\agent-worktree.ps1 status

# Safely remove clean task worktree
.\agent-worktree.ps1 remove -Role idea-spring
```
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene 开发者元工作区

该元仓库为所有 Cyrene 仓库提供统一、可复现的开发工作区配置。

---

## 1. 架构与策略

- **拓扑权威（Topology Authority）**：[`repositories.yaml`](repositories.yaml) 记录所有规范 Cyrene 仓库、相对路径、远端和构建系统。
- **CI 权威（CI Authority）**：[`docs/governance/ci-authority.md`](docs/governance/ci-authority.md) 记录每个仓库唯一的自动源码门禁及 Azure 补充范围。
- **不使用绝对路径**：所有工作区、解决方案和 IDE 配置均使用相对路径。
- **独立性保证**：每个仓库都能独立构建和测试，无需依赖 `Cyrene-Workspace`。
- **语言基线**：
  - **.NET**：.NET 10 LTS（`global.json`、`Cyrene.Workspace.slnx`）
  - **Python**：`>=3.11`（`uv`、`pyproject.toml`、`.python-version = 3.12`、仓库本地 `.venv`）
  - **Rust**：Cargo 工作区（`Cyrene-Platform/Cargo.toml`、`Cyrene-Services/Cyrene-Reactor/Cargo.toml`）
  - **JVM / Kotlin**：Gradle Kotlin DSL 和 Java Toolchains（Java 21/25、Kotlin 2.0+）

### 跨项目 API 命名宪法

所有 Cyrene 仓库共用由 Cyrene-Platform 管理的规范 API 词汇。跨仓库新增或重命名 API 前，请先阅读[《Cyrene API 命名宪法》](https://github.com/DoHorizon-AI/Cyrene-Platform/blob/develop/docs/governance/API_NAMING_CONSTITUTION.md)。Yield、Reactor、Exchange、Plugins 及其他 Product 仓库必须保留以下概念的语义区分：`Acquire`/`Reserve`、`Start`/`Launch`、`Stop`/`Terminate`、`Watch`/`Subscribe`，以及 `State`/`Status`/`Phase`。

Platform 是共享术语的语言来源。仓库不得为既有 Platform 概念添加本地同义词。任何有意保留的领域专属差异都必须记录在所属仓库中，并且不能形成第二个权威来源。首次公开发布前允许进行破坏性重命名；Cyrene 公共 API 命名冻结从该次发布开始。

### 规范仓库清单

| 仓库 | 相对路径 | 职责与技术 |
| :--- | :--- | :--- |
| `Cyrene-Platform` | `../Cyrene-Platform` | 核心平台内核、原生 sys/nvidia 运行时适配器、执行引擎和 Python SDK（以纯 Rust + Python 为基线；根据 PR #41 与 Product 控制平面解耦）|
| `Cyrene-Plugins-Official` | `../Cyrene-Plugins-Official` | 面向公开目标的能力合约、SDK、TCK 和可替换实现 |
| `Cyrene-Reactor` | `../Cyrene-Services/Cyrene-Reactor` | 推理部署 / 端点 Product，并直接消费执行引擎 |
| `Cyrene-Yield` | `../Cyrene-Services/Cyrene-Yield` | 分布式训练编排，以及 `ModelVersion` 的唯一权威 |
| `Cyrene-Exchange` | `../Cyrene-Services/Cyrene-Exchange` | 服务交换网关、分发合约和后端协调器 |
| `Cyrene-Catalyst` | `../Cyrene-Services/Cyrene-Catalyst` | 数据集整理，以及 `DatasetVersion` 的唯一权威 |
| `Cyrene-Echo` | `../Cyrene-Services/Cyrene-Echo` | 推理后评估，以及 `EvaluationRun` / `HumanAnnotation` / `FeedbackSet` 的唯一权威 |
| `Cyrene-Navigator` | `../Cyrene-Services/Cyrene-Navigator` | 桌面客户端体验，以及 `Conversation` / `AgentRun` 会话的唯一权威 |

> [!NOTE]
> **仓库与子组件边界**
> 上述 8 个规范仓库是 Cyrene 工作区中仅有的顶层 Git 仓库。
> 当前 Gradle 项目属于 Plugins 仓库，位于 `contracts/jvm` 和 `contracts/tck/model-provider-v1/jvm`；它们作为构建根链接，不是独立仓库。已退役的 Exchange 协调器、旧版 Spring 网关和 Platform JVM 控制平面路径仅供历史参考，绝不能重新加入 `repositories.yaml` 或 `.idea/jb-workspace.xml`。

---

## 2. 快速开始

### Windows（PowerShell）

```powershell
cd Cyrene-Workspace
.\bootstrap.ps1
.\verify.ps1
```

### Linux / macOS（Bash）

```bash
cd Cyrene-Workspace
./bootstrap.sh
```

---

## 3. 在 IDE 中打开

- **IntelliJ IDEA Ultimate / WebStorm / PyCharm / CLion**：
  打开 `Cyrene-Workspace` 文件夹。工作区通过 `.idea/jb-workspace.xml` 自动挂载 8 个规范仓库。
- **JetBrains Rider / Visual Studio**：
  打开 `Cyrene-Workspace/Cyrene.Workspace.slnx`。

手动验收清单请参阅 [`IDE_ACCEPTANCE.md`](IDE_ACCEPTANCE.md)。

---

## 4. 并行 Agent 工作树隔离

> **规范规则**：
> Agent 绝不能把其他 Agent 的可变工作树用作集成基线。应交换精确的远端 SHA，并使用脱离分支的快照。

### 用法

请使用规范仓库名称，或一对一且无歧义的便捷别名，例如 `platform`、`plugins`、`reactor`、`yield`、`exchange`、`catalyst`、`echo`、`navigator`：

```powershell
# 为写入 Agent 创建专用任务工作树
.\agent-worktree.ps1 create -Repo Cyrene-Plugins-Official -Branch chore/spring-boot-4-1-1 -Base origin/develop -Role idea-spring

# 为读取 Agent 创建脱离分支的不可变快照工作树
.\agent-worktree.ps1 snapshot -Repo Cyrene-Platform -Sha <40-char-sha> -Role rider-media-platform

# 查看活动 Agent 的工作树归属
.\agent-worktree.ps1 status

# 安全移除干净的任务工作树
.\agent-worktree.ps1 remove -Role idea-spring
```
