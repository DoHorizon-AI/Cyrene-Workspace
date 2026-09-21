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

---

## Wave 1 — Rust observability foundation

### 1. Implementation
- Created thin shared helper crate `framework/crates/cy-observability`.
- Modules implemented:
  - `config`: strict profile and log level validation.
  - `formatter`: custom `CyreneLayer` emitting strict NDJSON to stderr and human-readable text for development.
  - `redaction`: secret sanitization (tokens, keys, passwords, cookies, auth headers) and bounded truncation.
  - `panic_hook`: raw stderr emergency diagnostic output without locks or async queues.
  - `guard`: RAII guard for bounded non-blocking worker shutdown.
  - `init`: subscriber initialization directly to `std::io::stderr`.
- Integrated subscriber initialization into key Platform executables:
  - `cyrene-kernel/src/main.rs`
  - `cy-node-agent/src/main.rs`
  - `cy-runtime-agent/src/main.rs`
  - `cy-package-runtime/src/main.rs`
  - `cyrene-sandboxd/src/main.rs`

### 2. Evidence
- Command: `cargo test -p cy-observability`
  - Result: `13 passed; 0 failed; 0 ignored`
- Command: `cargo clippy -p cy-observability`
  - Result: 0 warnings, 0 errors
- Command: `cargo test -p cy-package-runtime`
  - Result: `5 passed` (including `package_runtime_tck`)
- Command: `cargo test -p cyrene-sandboxd`
  - Result: `18 passed`
- Command: `cargo test --workspace`
  - Result: All tests across the entire Platform workspace passed cleanly.

---

## Wave 2 — Error namespace and structured model

### 1. Implementation
- Canonical Platform error catalog: `PLATFORM.<DOMAIN>.<REASON>` in `cy_observability::error_catalog::PlatformErrorCode`.
- Structured NDJSON record schema implemented:
  - `schema_version`: 1
  - `timestamp`: UTC RFC3339
  - `level`: `TRACE` / `DEBUG` / `INFO` / `WARN` / `ERROR`
  - `event.name`: dot-separated stable string
  - `service.name`: logical service name
  - `service.instance.id`: unique instance UUID
  - `message`: human-readable description
  - `attributes`: key-value attributes
- Validated bounded constraints:
  - Max record budget: 32 KiB
  - Max message budget: 4 KiB
  - Log injection resistance: newlines escaped; single NDJSON line per record.

### 2. Evidence
- Command: `cargo test -p cy-observability -- test_structured_json_matches_specification_schema test_error_event_includes_stable_error_code test_sensitive_tokens_are_strictly_redacted test_overlong_message_is_safely_truncated test_log_injection_attempt_does_not_create_multiple_lines test_oversize_record_is_pruned_without_breaking_json`
  - Result: All test cases passed with positive and negative assertions.

---

## Wave 3 — Critical Platform instrumentation (Gate: OBS-G3)

### 1. Implementation
- **OBS-W3-A (Kernel Lease, Worker, Persistence & Recovery):**
  - In `cy-kernel-daemon` (`src/rpc/kernel_service.rs`): instrumented lease acquisition (`platform.lease.acquired`), lease release (`platform.lease.released`), lease release deferred/fail-closed (`platform.lease.release_deferred`), and lease rollback errors (`PLATFORM.KERNEL.JOURNAL_WRITE_FAILED`, `PLATFORM.LEASE.RELEASE_ROLLBACK_FAILED`, `PLATFORM.LEASE.RELEASE_INTENT_PERSIST_FAILED`).
  - In `cy-kernel-daemon` (`src/adapter/worker.rs`): instrumented watchdog reap (`platform.worker.reaped`) with worker ID and lease ID attributes.
- **OBS-W3-B (Execution Reconcile & Termination Classification):**
  - In `cy-execution-control` (`src/controller.rs`): instrumented transition to `UnknownRequiresReconciliation` on kernel invalid lease return or unknown `AssignmentAck` (`PLATFORM.WORKER.CLEANUP_UNCONFIRMED`, `PLATFORM.LEASE.RELEASE_INTENT_PERSIST_FAILED`).
- **OBS-W3-C (Node Disconnect, Reconnect & Backoff):**
  - In `cy-node-agent` (`src/daemon.rs`): implemented rate-limited connection diagnostics with retry count, elapsed time, backoff state. Emits `platform.node.connected`, `platform.node.disconnected`, `platform.node.reconnecting`, and `platform.node.reconnected` without log storming in retry loops.
- **OBS-W3-D (Sandbox Termination & Cleanup):**
  - In `cyrene-sandboxd` (`src/runtime.rs`): instrumented `kill_cgroup` failures, stale recovery cleanup, and cgroup directory removal warnings. Emits `platform.sandbox.terminated` with pid, cgroup path, and exit code.
- **OBS-W3-E (Package, Plugin & Adapter Lifecycle):**
  - In `cy-package-runtime` (`src/runtime.rs`): instrumented activation rollback failures (`PLATFORM.PACKAGE.DEACTIVATION_FAILED`), staging cleanup warnings, and lifecycle events (`platform.package.activated`, `platform.package.deactivated`) while preserving pristine JSON-RPC on stdout.
- **OBS-W3-F (Relay Failures & Workspace Fabric):**
  - In `cy-workspace-fabric` (`src/relay.rs`): instrumented session lifecycle (`platform.relay.connected`, `platform.relay.disconnected`), frame decoding errors (`platform.relay.frame_error`), and offline connector warnings without corrupting test fixture stdout contracts.

### 2. Evidence
- Commit: `0f9424e` in `Cyrene-Platform` (`feat(observability): implement critical platform instrumentation for Wave 3 (OBS-G3)`).
- Tests Executed:
  - `cargo test -p cy-kernel-daemon`: 82 passed; 0 failed
  - `cargo test -p cy-execution-control`: 20 passed; 0 failed
  - `cargo test -p cy-node-agent`: 13 passed; 0 failed
  - `cargo test -p cyrene-sandboxd`: 18 passed; 0 failed
  - `cargo test -p cy-package-runtime`: 5 passed; 0 failed
  - `cargo test -p cy-workspace-fabric`: 5 passed; 0 failed
  - `cargo test --workspace`: ALL crates across Platform workspace passed cleanly (0 failed).


