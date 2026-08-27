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

All 9 automated checks must report `[PASS]`.

---

## 2. IntelliJ IDEA Multi-Project Workspace Acceptance

### Step 1: Open Workspace
1. Launch **IntelliJ IDEA Ultimate** (2024.3+ / 2026.x).
2. Select **File $\rightarrow$ Open...**
3. Select the folder: `Cyrene-Workspace`.
4. Select **Trust Project**.

### Step 2: Verify Multi-Project Workspace (`.idea/jb-workspace.xml`)
1. **Multi-Project Hierarchy**:
   - In the Project view, verify each repository appears as an independent native project module:
     - `Cyrene-Platform` (Cargo workspace, Python SDKs, JVM control plane)
     - `plugins` (Python plugins, .NET compat, Spring gateway)
     - `services/cyrene-astrbot-rev` (.NET host, Python worker)
     - `services/cyrene-dh-system-internal` (.NET solution)
     - `services/cyrene-reactor` (Python runtime, Rust scheduler)
     - `services/Cyrene-Yield` (Python training engine)
     - `services/cyrene-exchange` (Python transport, JVM coordinator)
     - `services/cyrene-catalyst`, `cyrene-echo`, `cyrene-navigator`
2. **VCS Multi-Root Registration**:
   - Open **Git** tool window (`Alt + 9`).
   - Verify all 10 repositories are registered as distinct Git roots.
3. **Gradle Projects & Toolchain Auto-Provisioning**:
   - Open **Gradle** tool window.
   - Verify `Cyrene-Platform/framework/jvm`, `services/cyrene-exchange/components/coordinator`, and `plugins/plugins/gateway/spring` appear.
   - Verify Gradle wrapper 9.5.0 and Java 25 toolchain resolve automatically via `foojay-resolver-convention`.
4. **Rust Development Toolchain**:
   - Verify Cargo workspaces attach for `Cyrene-Platform` and `services/cyrene-reactor`.
   - Open a Rust source file (e.g. `Cyrene-Platform/kernel/crates/cy-kernel-daemon/src/main.rs`).
   - Verify trait/struct navigation and code completion work natively via rust-analyzer.
5. **Python Interpreters (.venv)**:
   - Verify each Python module binds to its own repo-local `.venv` (Python 3.12).
   - Open `services/Cyrene-Yield/training/core/src/cy_exec/training/artifacts.py` $\rightarrow$ press `Ctrl + B` on `ArtifactKind` to verify symbol navigation into `cy_artifacts`.

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
   - Verify 3 solution folders are present:
     - `/AstrBot/` (5 projects)
     - `/WeComAgentHub/` (18 projects)
     - `/Plugins/` (2 projects)
   - Total: **25 projects**.
2. **Cross-Project Semantic Navigation**:
   - Verify C# semantic search finds symbols across projects.
   - Verify **Find Usages** (`Alt + F7`) and **Go to Implementation** work across solution projects.
3. **Unit Test Discovery**:
   - Open **Unit Tests** tool window (`Alt + 8`).
   - Verify tests from `AstrBot.DotNetHost.Tests` and `WeComAgentHub.*.Tests` are discovered.
4. **Aggregate Build**:
   - Select **Build $\rightarrow$ Build Solution** (`Ctrl + Shift + B`).
   - Verify build finishes with **0 warnings and 0 errors**.

---

## 4. Acceptance Checklist

| Check Item | Target Tool | Expected Result | Verified |
|---|---|---|:---:|
| Automated Gate | PowerShell | `.\verify.ps1` reports 9/9 Passed | [ ] |
| Multi-Project Model | IntelliJ IDEA | `jb-workspace.xml` loads 10 independent projects | [ ] |
| VCS Multi-Root | IntelliJ IDEA | All 10 Git repositories recognized | [ ] |
| Gradle Baseline | IntelliJ IDEA | Gradle 9.5.0 + Kotlin 2.4.10 + JDK 25 resolve | [ ] |
| Cargo Attach | IntelliJ IDEA / RustRover | Platform & Reactor Cargo workspaces recognized | [ ] |
| Python .venv Binding | IntelliJ IDEA | All 6 repos bind to respective `.venv` (3.12) | [ ] |
| Python Navigation | IntelliJ IDEA | Cross-package symbols (`cy_artifacts`, etc.) resolve | [ ] |
| .NET Project Tree | JetBrains Rider | All 25 projects load in `Cyrene.Workspace.slnx` | [ ] |
| .NET Semantic Search | JetBrains Rider | Symbol search & Find Usages work across projects | [ ] |
| .NET Test Explorer | JetBrains Rider | Tests from AstrBot and WeComAgentHub discovered | [ ] |
| .NET Aggregate Build | JetBrains Rider | Clean build of all 25 projects with 0 errors | [ ] |
| IDE Re-open Persistence | IDEA & Rider | Zero re-configuration required upon reopening | [ ] |
