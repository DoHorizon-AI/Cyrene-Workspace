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
  - **Rust**: Cargo workspaces (`Cyrene-Platform/Cargo.toml`, `Services/Cyrene-Reactor/Cargo.toml`)
  - **JVM / Kotlin**: Gradle Kotlin DSL with Java Toolchains (Java 21/25, Kotlin 2.0+)

### Canonical Repository Inventory

| Repository | Relative Path | Role & Technology |
| :--- | :--- | :--- |
| `Cyrene-Platform` | `../Cyrene-Platform` | Core platform kernel, native sys/nvidia runtime adapters, execution engine, and Python SDK (Pure Rust + Python baseline; decoupled from product control-planes per PR #41) |
| `Cyrene-Plugins-Official` | `../Cyrene-Plugins-Official` | Official connectors, extensions, media processors, and gateway spring integrations |
| `Cyrene-Reactor` | `../Services/Cyrene-Reactor` | High-throughput inference runtime service (Python + Rust scheduler) |
| `Cyrene-Yield` | `../Services/Cyrene-Yield` | Distributed training orchestration and sole authority for `ModelVersion` |
| `Cyrene-Exchange` | `../Services/Cyrene-Exchange` | Service exchange gateway, dispatch contracts, and backend coordinator |
| `Cyrene-Catalyst` | `../Services/Cyrene-Catalyst` | Dataset curation and sole authority for `DatasetVersion` |
| `Cyrene-Echo` | `../Services/Cyrene-Echo` | Post-inference evaluation and sole authority for `EvaluationRun` / `HumanAnnotation` / `FeedbackSet` |
| `Cyrene-Navigator` | `../Services/Cyrene-Navigator` | Desktop client experience and sole authority for `Conversation` / `AgentRun` sessions |

> [!NOTE]
> **Repository vs. Subcomponent Boundary**  
> Only the 8 canonical repositories above are top-level Git repositories in the Cyrene workspace.  
> Internal submodules or build components—such as `coordinator` (`Services/Cyrene-Exchange/components/coordinator`) or `spring` (`Cyrene-Plugins-Official/plugins/gateway/spring`)—are Gradle/build subcomponents belonging to their respective parent repositories. They are linked via build systems (`.idea/gradle.xml`, `settings.gradle.kts`) and must **never** be registered as standalone repositories in `repositories.yaml` or as top-level `<project>` entries in `.idea/jb-workspace.xml`.  
> *Note on JVM Control-Plane*: The legacy `cyrene-control-plane` JVM service (`framework/jvm`) previously inside `Cyrene-Platform` was retired in PR #41 to maintain a clean platform substrate boundary; its product control-plane surfaces migrated to domain-owned services (e.g. `Services/Cyrene-Yield`).

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
