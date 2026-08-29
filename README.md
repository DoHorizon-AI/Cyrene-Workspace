# Cyrene Developer Meta-Workspace

This meta-repository provides a unified, reproducible developer workspace configuration for all Cyrene repositories.

---

## 1. Architecture & Policy

- **Topology Authority**: [`repositories.yaml`](repositories.yaml) records all canonical Cyrene repositories, relative paths, remotes, and build systems.
- **Zero Absolute Paths**: All workspace, solution, and IDE configurations use relative paths.
- **Independence Guarantee**: Each repository remains fully buildable and testable on its own without requiring `Cyrene-Workspace`.
- **Language Baselines**:
  - **.NET**: .NET 10 LTS (`global.json`, `Cyrene.Workspace.slnx`)
  - **Python**: `>=3.11` (`uv`, `pyproject.toml`, `.python-version = 3.12`, repo-local `.venv`)
  - **Rust**: Cargo workspaces (`Cyrene-Platform/Cargo.toml`, `Services/Cyrene-Reactor/Cargo.toml`)
  - **JVM / Kotlin**: Gradle Kotlin DSL with Java Toolchains (Java 21/25, Kotlin 2.0+)

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
  Open the `Cyrene-Workspace` folder.
- **JetBrains Rider / Visual Studio**:
  Open `Cyrene-Workspace/Cyrene.Workspace.slnx`.

See [`IDE_ACCEPTANCE.md`](IDE_ACCEPTANCE.md) for the manual acceptance checklist.

---

## 4. Parallel Agent Worktree Isolation

> **Canonical Rule**:
> Agents must never use another Agent's mutable working tree as an integration baseline. Exchange exact remote SHAs and use detached snapshots.

### Usage

```powershell
# Create dedicated task worktree for writer agent
.\agent-worktree.ps1 create -Repo plugins -Branch chore/spring-boot-4-1-1 -Base origin/develop -Role idea-spring

# Create detached immutable snapshot worktree for consumer agent
.\agent-worktree.ps1 snapshot -Repo Cyrene-Platform -Sha <40-char-sha> -Role rider-media-platform

# View active agent worktree ownership
.\agent-worktree.ps1 status

# Safely remove clean task worktree
.\agent-worktree.ps1 remove -Role idea-spring
```

