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

> [!NOTE]
> **Repository vs. Subcomponent Boundary**  
> Only the 8 canonical repositories above are top-level Git repositories in the Cyrene workspace.  
> Current Gradle projects belong to the Plugins repository under `contracts/jvm` and `contracts/tck/model-provider-v1/jvm`; they are linked as build roots, not as standalone repositories. The retired Exchange coordinator, legacy Spring gateway, and Platform JVM control-plane paths are historical references only and must not be reintroduced into `repositories.yaml` or `.idea/jb-workspace.xml`.

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
  Open the `Cyrene-Workspace` folder. The workspace automatically mounts the 8 canonical repositories via `.idea/jb-workspace.xml`.
- **JetBrains Rider / Visual Studio**:
  Open `Cyrene-Workspace/Cyrene.Workspace.slnx`.

See [`IDE_ACCEPTANCE.md`](IDE_ACCEPTANCE.md) for the manual acceptance checklist.

---

## 4. Parallel Agent Worktree Isolation

> **Canonical Rule**:  
> Agents must never use another Agent's mutable working tree as an integration baseline. Exchange exact remote SHAs and use detached snapshots.

### Usage

Use canonical repository names (or clean 1:1 convenience aliases like `platform`, `plugins`, `reactor`, `yield`, `exchange`, `catalyst`, `echo`, `navigator`):

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
