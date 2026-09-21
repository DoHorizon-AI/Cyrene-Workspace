# Evidence — RC Logging, Error Codes & Diagnostics System (2026-09-21)

> Status: IN_PROGRESS — Wave 0 completed and verified on local development host.
> 状态：进行中 — Wave 0 基线与矩阵已核验。

## Environment / 环境

| Field | Value |
| --- | --- |
| OS | Linux 6.6.137.1-microsoft-standard-WSL2 x86_64 |
| Toolchain | rustc 1.85.0 / cargo 1.85.0 (edition 2021/2024 supported) |
| Python | 3.12.3 |
| Git branch | `docs/logging-and-errors-spec` (diverged from `develop`) |
| GPU | NVIDIA GeForce RTX 5070 (driver 616.92) |
| Hosted CI | NOT_RUN (local verification only) |

---

## Wave 0 — Reconfirm implementation baseline

### 1. Specification & Topology Alignment
- Read canonical specification `Cyrene-Workspace/docs/standards/logging-and-errors.md` (623 lines).
- Confirmed synchronized copies across `Cyrene-Platform`, `Cyrene-Plugins-Official`, `Cyrene-Studio`, and `Cyrene-Services/*`.
- Verified working tree cleanliness:
  - `Cyrene-Platform`: 1 uncommitted file `adapters/hardware/nvidia/src/main.rs` (preserved intact, tests pass).
  - All other repositories (`Cyrene-Workspace`, `Cyrene-Plugins-Official`, `Cyrene-Studio`, `Cyrene-Services/*`): clean on branch `docs/logging-and-errors-spec`.

### 2. Binary / Library & Boundary Classification
- Audited all executables vs libraries in `Cyrene-Platform`:
  - Executables: `cyrene-kernel`, `cy-node-agent`, `cy-runtime-agent`, `cyrene-sandboxd`, `cy-package-runtime`, `cyrene-nvidia-adapter`, `cyrene-linux-sys-adapter`.
  - Machine protocol on stdout: `cy-package-runtime` (JSON-RPC over stdin/stdout), `cy-platform-api` (JSON output on stdout for capability resolver), `cy-manifest` (hash on stdout). All diagnostic logs MUST be directed explicitly to stderr.
  - Journal authority: `FileRuntimeJournal` in `cyrene-kernel` records durable epoch and fence tokens. Diagnostic logging does NOT replace or mutate this journal.
  - Audit authority: Product-owned SQLite `RequestAudit` store in `Cyrene-Exchange` records request execution and token quotas.
  - Event authority: Product event streams (SSE/WebSocket) in `Cyrene-Reactor` and `Cyrene-Yield` for training progress and model serving state.

### 3. Ignored Result (`let _ =`) Classification
- Audited 70+ occurrences of `let _ =` and unhandled errors across Platform:
  - `SAFETY_CRITICAL`: `kernel_service.rs` rollback of in-memory lease on journal failure; `fail_release` error suppression; `controller.rs` transition to `UnknownRequiresReconciliation`.
  - `BEST_EFFORT`: sandbox process killing (`libc::kill`), cgroup directory cleanup (`fs::remove_dir`), temporary directory removal on staging failures.
  - `EXPECTED_BENIGN`: gRPC sender dropped on client cancelation, closed channel send during termination.
  - `RECOVERABLE_FAILURE`: `cy-node-agent` disconnect/reconnect backoff loop.

### 4. Gate OBS-G0 Verification
- Plan document: `Cyrene-Workspace/docs/plans/rc-logging-and-diagnostics.md` created.
- Evidence record: `Cyrene-Workspace/docs/plans/evidence/2026-09-21-rc-logging-diagnostics.md` created.
- Zero production code behavior modified in Wave 0.
