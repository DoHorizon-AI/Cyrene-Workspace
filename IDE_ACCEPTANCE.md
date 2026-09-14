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
