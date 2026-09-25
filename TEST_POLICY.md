# Cyrene Developer Agent Validation Policy

This document defines the three standard validation tiers for developer agent tasks in the Cyrene Workspace appliance.

---

## 1. Validation Tiers

```
  ┌────────────────────────────────────────────────────────┐
  │                 L1: FOCUSED GATE                       │
  │  Fast edit-loop validation (single unit/file test)     │
  │  Target wall-clock: < 5 seconds                        │
  └──────────────────────────┬─────────────────────────────┘
                             │
                             ▼
  ┌────────────────────────────────────────────────────────┐
  │              L2: TASK / SUBSYSTEM GATE                 │
  │  Affected module / contract / TCK gate before delivery │
  │  Target wall-clock: < 30 seconds                       │
  └──────────────────────────┬─────────────────────────────┘
                             │
                             ▼
  ┌────────────────────────────────────────────────────────┐
  │              L3: FULL INTEGRATION GATE                 │
  │  PR / merge / release / explicitly high-risk tasks     │
  │  Executed via CI or explicit pre-release verification  │
  └────────────────────────────────────────────────────────┘
```

### L1 — FOCUSED (Edit-Loop Only)
- **Scope**: Only the specific unit test, function, or file currently being edited.
- **When to Run**: Continuously during the inner editing loop.
- **Commands**:
  - Rust: `cargo test -p <crate> --test <test_name> -- <filter>`
  - Python: `pytest <path_to_test_file>::<test_func>`
  - .NET: `dotnet test <project_path> --filter "FullyQualifiedName=<name>"`

### L2 — TASK / SUBSYSTEM (Contract & TCK Gate)
- **Scope**: All unit tests, contracts, schema generators, and TCK cases for the affected subsystem/crate.
- **When to Run**: Prior to task commit and branch push.
- **Commands**:
  - Platform/Rust: `cargo test -p cy-proto`, `cargo test -p cy-platform-api --test <subsystem_tck>`
  - Contracts: `pwsh -NoProfile -File ./scripts/contract-harness.ps1 -ProtoFile <path>`
  - Plugins: `pytest plugins/<plugin_dir>/tests/`
  - .NET: `dotnet test solutions/<solution_name>.slnx`

### L3 — FULL INTEGRATION (Release & High-Risk Gate)
- **Scope**: Multi-repository end-to-end integration, full solution compilation, all 1000+ workspace tests.
- **When to Run**: Only during PR review, release qualification, or explicitly requested high-risk multi-repo refactoring.
- **Commands**:
  - `pwsh -NoProfile -File ./verify.ps1`
  - CI workflow execution (`ubuntu-latest` Linux canonical runtime gate)

---

## 2. Environment Fault Recording Policy

If an L3 or broad test run reports unrelated known environment failures (such as missing optional external database daemons, network authentication timeouts, or OS-specific runtime limitations):
1. **Record the diagnostic**: Document the failure in the J-Space ledger or task evidence ledger.
2. **Do NOT silently ignore**: State clearly why the failure is an environment constraint outside the task's scope.
3. **Do NOT rerun repeatedly**: Ordinary feature tasks must NOT loop or retry full integration suites for known unaffected components.
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene 开发者 Agent 验证策略

本文定义 Cyrene Workspace 开发环境中 Agent 任务的三个标准验证层级。

---

## 1. 验证层级

```
  ┌────────────────────────────────────────────────────────┐
  │                 L1：定向门禁                            │
  │  快速编辑循环验证（单个单元 / 文件测试）                 │
  │  目标用时：< 5 秒                                       │
  └──────────────────────────┬─────────────────────────────┘
                             │
                             ▼
  ┌────────────────────────────────────────────────────────┐
  │              L2：任务 / 子系统门禁                      │
  │  交付前验证受影响模块 / 合约 / TCK                       │
  │  目标用时：< 30 秒                                      │
  └──────────────────────────┬─────────────────────────────┘
                             │
                             ▼
  ┌────────────────────────────────────────────────────────┐
  │              L3：完整集成门禁                           │
  │  PR / 合并 / 发布 / 明确要求的高风险任务                 │
  │  通过 CI 或明确的发布前验证执行                           │
  └────────────────────────────────────────────────────────┘
```

### L1 — 定向验证（仅编辑循环）
- **范围**：仅检查当前正在编辑的特定单元测试、函数或文件。
- **运行时机**：在内部编辑循环中持续运行。
- **命令**：
  - Rust：`cargo test -p <crate> --test <test_name> -- <filter>`
  - Python：`pytest <path_to_test_file>::<test_func>`
  - .NET：`dotnet test <project_path> --filter "FullyQualifiedName=<name>"`

### L2 — 任务 / 子系统验证（合约与 TCK 门禁）
- **范围**：运行受影响子系统 / crate 的全部单元测试、合约、schema 生成器和 TCK 用例。
- **运行时机**：提交任务和推送分支之前。
- **命令**：
  - Platform/Rust：`cargo test -p cy-proto`、`cargo test -p cy-platform-api --test <subsystem_tck>`
  - Contracts：`pwsh -NoProfile -File ./scripts/contract-harness.ps1 -ProtoFile <path>`
  - Plugins：`pytest plugins/<plugin_dir>/tests/`
  - .NET：`dotnet test solutions/<solution_name>.slnx`

### L3 — 完整集成验证（发布与高风险门禁）
- **范围**：多仓端到端集成、完整解决方案编译，以及 Workspace 全部 1000+ 个测试。
- **运行时机**：仅用于 PR 审查、发布资格确认，或明确要求的高风险多仓重构。
- **命令**：
  - `pwsh -NoProfile -File ./verify.ps1`
  - CI 工作流执行（`ubuntu-latest` 规范运行时门禁）

---

## 2. 环境故障记录策略

如果 L3 或范围较广的测试运行报告了已知且与任务无关的环境故障（例如缺少可选外部数据库服务、网络认证超时或操作系统运行时限制）：
1. **记录诊断信息**：将故障记录在 J-Space 台账或任务证据台账中。
2. **不得悄悄忽略**：明确说明该故障为何属于任务范围之外的环境限制。
3. **不要反复重试**：普通功能任务不得针对已知且不受影响的组件循环重试完整集成套件。
