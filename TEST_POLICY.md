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
