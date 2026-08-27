# Cyrene Multi-Repository Workspace Manual IDE Acceptance Guide

This document defines the manual verification procedure to validate that the Cyrene multi-repository developer environment correctly loads in **IntelliJ IDEA Ultimate** and **JetBrains Rider**.

---

## 1. Prerequisites & Toolchain Verification

Before launching the IDEs, ensure the local toolchain is verified by running:

```powershell
cd C:\Users\Baiji\DHDev\Cyrene\Cyrene-Workspace
.\bootstrap.ps1
.\verify.ps1
```

All 8 automated verification checks must report `[PASS]`.

---

## 2. IntelliJ IDEA Multi-Project Workspace Acceptance

### Step 1: Open Workspace
1. Launch **IntelliJ IDEA Ultimate** (2024.3+).
2. Select **File $\rightarrow$ Open...**
3. Select the folder: `Cyrene-Workspace` (`C:\Users\Baiji\DHDev\Cyrene\Cyrene-Workspace`).
4. Select **Trust Project**.

### Step 2: Verify Project & Build System Attachments
1. **VCS Mappings**:
   - Open **Git** tool window (`Alt + 9` / `Cmd + 9`).
   - Verify all 10 repositories (`Cyrene-Platform`, `plugins`, `services/cyrene-astrbot-rev`, `services/cyrene-dh-system-internal`, `services/cyrene-reactor`, `services/Cyrene-Yield`, `services/cyrene-exchange`, `services/cyrene-catalyst`, `services/cyrene-echo`, `services/cyrene-navigator`) are registered as active Git roots.
2. **Gradle Projects**:
   - Open **Gradle** tool window.
   - Verify `Cyrene-Platform/framework/jvm`, `services/cyrene-exchange/components/coordinator`, and `plugins/plugins/gateway/spring` appear as attached Gradle projects.
   - Verify Gradle sync completes using the declared Java toolchain without requiring manual local JDK path configuration.
3. **Rust Workspaces (Rust Plugin / RustRover integration)**:
   - Open **Cargo** tool window.
   - Verify `Cyrene-Platform/Cargo.toml` and `services/cyrene-reactor/Cargo.toml` attach automatically.
   - Open a Rust source file (e.g., `Cyrene-Platform/kernel/crates/cy-kernel-daemon/src/main.rs`).
   - Verify trait/struct navigation and code completion work natively.
4. **Python Interpreters (.venv)**:
   - Open **Project Structure** (`Ctrl + Alt + Shift + S`) $\rightarrow$ **SDKs** / **Modules**.
   - Verify each Python repository is bound to its repository-local virtual environment:
     - `Cyrene-Platform` $\rightarrow$ `Cyrene-Platform/.venv/Scripts/python.exe`
     - `services/cyrene-reactor` $\rightarrow$ `services/cyrene-reactor/.venv/Scripts/python.exe`
     - `services/Cyrene-Yield` $\rightarrow$ `services/Cyrene-Yield/.venv/Scripts/python.exe`
     - `services/cyrene-exchange` $\rightarrow$ `services/cyrene-exchange/.venv/Scripts/python.exe`
     - `services/cyrene-astrbot-rev` $\rightarrow$ `services/cyrene-astrbot-rev/python/capability_worker/.venv/Scripts/python.exe`
     - `plugins` $\rightarrow$ `plugins/.venv/Scripts/python.exe`
   - Open a Python file (e.g., `services/Cyrene-Yield/training/core/src/cy_exec/training/artifacts.py`).
   - Press `Ctrl + B` on `ArtifactKind` $\rightarrow$ verifies jump to definition in `cy_artifacts`.

### Step 3: Test Entrypoints
1. Open a test file in any Python module (e.g. `services/Cyrene-Yield/training/core/tests/test_artifact_plane.py`).
2. Verify the gutter test run icon (green play button) appears and pytest executes successfully.

### Step 4: Session Persistence
1. Close IntelliJ IDEA completely (`File $\rightarrow$ Exit`).
2. Re-open IntelliJ IDEA to `Cyrene-Workspace`.
3. Verify all VCS roots, Gradle projects, Cargo projects, and Python interpreters remain attached without manual configuration.

---

## 3. JetBrains Rider .NET Solution Acceptance

### Step 1: Open Solution
1. Launch **JetBrains Rider** (2024.3+).
2. Select **File $\rightarrow$ Open...**
3. Select `Cyrene-Workspace/Cyrene.Workspace.slnx` (`C:\Users\Baiji\DHDev\Cyrene\Cyrene-Workspace\Cyrene.Workspace.slnx`).

### Step 2: Verify Solution Hierarchy & Semantic Indexing
1. **Solution Explorer Structure**:
   - Verify 3 solution folders are present:
     - `/AstrBot/` (5 projects: `AstrBot.DotNetHost`, `AstrBot.DatabaseMigrator`, `IrisHistoryImporter`, `AstrBot.KnowledgeBaseImporter`, `AstrBot.DotNetHost.Tests`)
     - `/WeComAgentHub/` (18 projects: `WeComAgentHub.Domain`, `WeComAgentHub.Application`, `WeComAgentHub.Infrastructure`, `WeComAgentHub.Wecom`, `WeComAgentHub.Mcp`, `WeComAgentHub.Api`, `WeComAgentHub.AdminWeb`, `WeComAgentHub.AppHost`, `WeComAgentHub.Scheduler`, `WeComAgentHub.Worker`, CloudConnectors, Tests)
     - `/Plugins/` (2 projects: `Cyrene.AgentSystem.Compat`, `Cyrene.AspNetCoreGateway`)
2. **Cross-Project Semantic Navigation**:
   - In `WeComAgentHub.Api`, navigate to a domain entity class (e.g., in `WeComAgentHub.Domain`).
   - Use **Find Usages** (`Alt + F7`) and **Go to Symbol** (`Ctrl + Alt + Shift + T`).
   - Verify symbols from `AstrBot` and `WeComAgentHub` resolve with full type information and syntax highlighting.
3. **Unit Test Discovery**:
   - Open **Unit Tests** tool window (`Alt + 8`).
   - Verify tests from both `AstrBot.DotNetHost.Tests` and `WeComAgentHub.*.Tests` appear in the test tree.
4. **Aggregate Build**:
   - Select **Build $\rightarrow$ Build Solution** (`Ctrl + Shift + B`).
   - Verify the build completes successfully with 0 errors.

---

## 4. Acceptance Checklist

| Check Item | Target Tool | Expected Result | Verified |
|---|---|---|:---:|
| Automated Gate | PowerShell | `.\verify.ps1` reports 8/8 Passed | [ ] |
| VCS Multi-Root | IntelliJ IDEA | All 10 Git repositories recognized | [ ] |
| Gradle Import | IntelliJ IDEA | JVM projects load without manual JDK configuration | [ ] |
| Cargo Attach | IntelliJ IDEA / RustRover | Platform & Reactor Cargo workspaces recognized | [ ] |
| Python .venv Binding | IntelliJ IDEA | All 6 repos bind to respective `.venv` interpreters | [ ] |
| Python Navigation | IntelliJ IDEA | Cross-package symbols (`cy_artifacts`, etc.) resolve | [ ] |
| .NET Project Tree | JetBrains Rider | All 25 projects load in `Cyrene.Workspace.slnx` | [ ] |
| .NET Semantic Search | JetBrains Rider | Symbol search & Find Usages work across projects | [ ] |
| .NET Test Explorer | JetBrains Rider | Tests from AstrBot and WeComAgentHub discovered | [ ] |
| .NET Aggregate Build | JetBrains Rider | Clean build of all 25 projects with 0 errors | [ ] |
| IDE Re-open Persistence | IDEA & Rider | Zero re-configuration required upon reopening | [ ] |
