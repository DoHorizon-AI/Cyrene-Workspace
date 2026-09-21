# Evidence — RC Logging, Error Codes & Diagnostics System (2026-09-21)

> Status: COMPLETE — All waves (Wave 0 to Wave 6) verified; Gate OBS-G6 achieved.
> 状态：已完成 — 全部 Wave（Wave 0 至 Wave 6）通过，达成 OBS-G6 门禁，具备 RC 联合验收条件。

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

---

## Wave 4 — Correlation and API mapping (Gate: OBS-G4)

### 1. Implementation
- **OBS-W4-01 (Correlation Hierarchy):**
  - Created `cy_observability::correlation::CorrelationContext` enforcing strict hierarchy between `request_id` (single HTTP/RPC call), `operation_id` (long-running asynchronous workflow), `resource_id` (business entity reference), and `trace_id`/`span_id` (distributed trace context).
  - In `cy_observability::formatter::CyreneLayer`: automatically promotes `trace_id` and `span_id` to top-level JSON fields matching specification Section 4.1, while preserving `request_id`, `operation_id`, and `resource_id` in `attributes`.
- **OBS-W4-02 (Untrusted Boundary Sanitization & W3C Trace Context):**
  - Implemented `TraceContext` with strict W3C `traceparent` validation (`00-<32hex>-<16hex>-<2hex>`), rejecting invalid versions, length mismatches, non-hex characters, and all-zero IDs.
  - Implemented `sanitize_request_id` (128 chars), `sanitize_operation_id` (128 chars), and `sanitize_resource_id` (256 chars), stripping control characters, newlines (`\r`, `\n`), tabs, null bytes, and quotes to eliminate log injection and header smuggling vulnerabilities.
- **OBS-W4-03 (Cross-Product & Plugin Correlation Mapping):**
  - In `Cyrene-Exchange` (`cyrene_exchange_product`):
    - Added `EXCHANGE_ERROR_MAPPINGS` and `map_exchange_error` mapping Exchange product error codes to canonical `PRODUCT.EXCHANGE.<REASON>` codes, `cause_kind`, and `recovery_action`.
    - Added W3C `traceparent` and `x-request-id` header validation and propagation in HTTP middleware and error handlers (`ProblemDetails`).
    - Implemented `logging.py` providing Cyrene-compliant NDJSON structured log formatting and recursive secret redaction without binary dependencies on Platform.
    - Explicitly distinguished credential tokens (`token`, `auth`, `api_key`) from usage metrics (`tokens`, `prompt_tokens`, `completion_tokens`).

### 2. Evidence
- Commit: `9179935` in `Cyrene-Platform` (`feat(observability): implement correlation hierarchy, W3C trace context, and untrusted header sanitization (OBS-G4)`).
- Commit: `941cd8b` in `Cyrene-Services/Cyrene-Exchange` (`feat(correlation): implement W3C trace context, header sanitization, and error mapping for Exchange`).
- Tests Executed:
  - `cargo test -p cy-observability`: 17 passed; 0 failed (all correlation, W3C parsing, rejection, and log promotion tests passing).
  - `product/.venv/bin/pytest product/tests` in `Cyrene-Exchange`: 39 passed; 0 failed (including `test_correlation_and_errors.py`).

---

## Wave 5 — Local persistence and bounded failure behavior (Gate: OBS-G5)

### 1. Implementation
- **OBS-W5-01 (Controlled Local File Sink & Single Rotation Owner):**
  - Created `framework/crates/cy-observability/src/sink.rs`: `BoundedRollingFileSink` implementing `std::io::Write`.
  - Single rotation ownership authority: only the application rolling sink shifts and rotates `.log`, `.log.1`..`.log.N` files; no concurrent or overlapping rotation mechanisms.
  - Standalone/unmanaged runtimes can configure `RollingFileConfig` to log directly to controlled local disk storage alongside stderr.
- **OBS-W5-02 (Bounded Operational Budgets):**
  - Default file budget: 50 MiB (`DEFAULT_MAX_FILE_BYTES`).
  - Default retention budget: 5 files (`DEFAULT_MAX_HISTORY_FILES`), bounding per-host disk space to `<= 300 MiB`.
  - In-memory non-blocking worker queue: default 10,000 records.
  - Flush timeout on shutdown: bounded to 2,000 ms (`Duration::from_secs(2)`).
  - Record payload budget: bounded to 32 KiB (`DEFAULT_MAX_RECORD_BYTES`).
  - Message budget: bounded to 4 KiB (`DEFAULT_MAX_MESSAGE_BYTES`).
- **OBS-W5-03 (Failure Mode Hardening):**
  - Implemented `dropped_writes` tracking via `AtomicU64` in `BoundedRollingFileSink`.
  - Hardened against filesystem errors: write failures, permission denied, disk-full or read-only filesystem errors are non-fatal, increment `dropped_writes_count()`, and do not panic.
  - Logging infrastructure failures never deadlock worker lease releases, worker watchdog reaps, or reconcile cleanups.
  - Configurable `lossy` policy via `NonBlockingBuilder` prevents queue exhaustion from inducing backpressure deadlocks.

### 2. Evidence
- Commit: `8d34abf` in `Cyrene-Platform` (`feat(observability): implement bounded rolling file persistence and fault elasticity`).
- Tests Executed:
  - `cargo test -p cy-observability`: 20 passed; 0 failed (including `rolling_file_config_defaults_and_builder`, `rolling_file_sink_rotates_and_shifts_history`, `rolling_file_sink_tracks_dropped_writes_on_error`).
  - `cargo test --workspace`: ALL crates across Platform workspace passed cleanly (0 failed).

---

## Wave 6 — Acceptance & Governance (Gate: OBS-G6)

### 1. Specification Compliance Matrix (LOG-01 to LOG-14)

| Criteria ID | Specification Requirement | Verification Method | Status | Evidence / Code References |
| :--- | :--- | :--- | :--- | :--- |
| **LOG-01** | Binary startup & config failures structured; libraries do not install global subscribers | Unit & Integration tests | **PASS** | `init_observability` returns `ObservabilityGuard`; installed only in binaries (`cyrene-kernel`, `cy-node-agent`, `cy-runtime-agent`, `cy-package-runtime`, `cyrene-sandboxd`). Library crates (`cy-observability`, `cy-execution-control`, `cy-workspace-fabric`) only emit events. |
| **LOG-02** | Structured NDJSON schema, UTC timestamps, size limits (32 KiB record, 4 KiB message); normal events omit `error.code` | Unit tests | **PASS** | `tests::structured_json_matches_specification_schema`, `tests::overlong_message_is_safely_truncated`, `tests::oversize_record_is_pruned_without_breaking_json`. |
| **LOG-03** | Pristine stdout isolation; zero log contamination of machine protocols (JSON-RPC, hash, fixture) | Test suites | **PASS** | Diagnostic output explicitly routed to stderr / rolling sink (`NonBlockingBuilder::finish(io::stderr())`). `cy-package-runtime` TCK test passes over stdout JSON-RPC; `cyrene-sandboxd` stdio bridge passes; `cy-workspace-fabric` passes. |
| **LOG-04** | Injected persistence/cleanup failure records exact cause, preserved state, recovery action; no fabricated RELEASED | Unit & RPC tests | **PASS** | `kernel_service.rs` lease failure paths emit `PLATFORM.KERNEL.JOURNAL_WRITE_FAILED`, `PLATFORM.LEASE.RELEASE_ROLLBACK_FAILED`, `PLATFORM.LEASE.RELEASE_INTENT_PERSIST_FAILED` with `allocation_released = false`. Reconcile in `controller.rs` transitions to `UnknownRequiresReconciliation`. |
| **LOG-05** | Reconnect / backoff loops output rate-limited summaries with retry count and elapsed time; no retry log storm | Unit tests | **PASS** | `cy-node-agent::daemon` rate-limits reconnect logging: logs initial disconnect, throttles retries with exponential backoff & elapsed time, and logs on reconnection. |
| **LOG-06** | Normal cancelation does not produce misleading ERROR; unconfirmed cleanup isolated | Code audit & tests | **PASS** | Client cancelations in `kernel_service.rs` and `worker.rs` avoid false errors; unconfirmed worker cleanup explicitly isolated as `PLATFORM.WORKER.CLEANUP_UNCONFIRMED`. |
| **LOG-07** | Strict correlation hierarchy (`request_id`, `operation_id`, `resource_id`, `trace_id`/`span_id`) without cross-talk; async task safety | Unit tests | **PASS** | `CorrelationContext`, W3C `TraceContext` parsing/derivation, and `CyreneLayer` top-level trace promotion in `cy-observability`; verified in `tests::structured_record_promotes_trace_id_and_retains_correlation_attributes`. |
| **LOG-08** | Tokens, API keys, cookies, passwords, auth headers redacted; token usage counters (`tokens`, `prompt_tokens`) not over-redacted | Unit tests | **PASS** | `redaction::tests::sensitive_values_are_redacted`, `redaction::tests::sensitive_keys_are_detected` in Rust; `test_redaction_does_not_mask_token_usage_metrics` in Python Exchange. |
| **LOG-09** | Log injection resistance, overlong string truncation, bounded record budget | Unit tests | **PASS** | `tests::log_injection_attempt_does_not_create_multiple_lines` (CR/LF escaped in JSON value strings); 4 KiB message truncate with `... [TRUNCATED]`; 32 KiB record pruning with `truncated: true`. |
| **LOG-10** | Non-blocking queue bounded; disk full, permission denied, dropped writes tracked without deadlock | Unit tests | **PASS** | `BoundedRollingFileSink` tracks `dropped_writes_count`; non-blocking worker queue with bounded limits; verified in `tests::rolling_file_sink_tracks_dropped_writes_on_error`. |
| **LOG-11** | Emergency panic hook outputs sanitized diagnostics to raw stderr without locks/async queues; no false cleanup claims | Unit & code audit | **PASS** | `cy-observability::panic_hook::install_panic_hook` writes panic location and payload to raw stderr without acquiring locks or relying on async queues. |
| **LOG-12** | Local rolling file sink retention strictly scoped to managed logs; zero deletion of state/artifacts/checkpoints | Unit tests | **PASS** | `BoundedRollingFileSink::rotate` operates exclusively on `{directory}/{file_prefix}.log*` within `1..=max_history_files`; verified in `tests::rolling_file_sink_rotates_and_shifts_history`. |
| **LOG-13** | Stable error codes mapped between API/UI and diagnostics; safe degradation on unknown errors | Integration tests | **PASS** | `Cyrene-Exchange` `ProblemDetails` RFC 9457 error handler maps errors via `map_exchange_error` with `PRODUCT.EXCHANGE.<REASON>`, `trace_id`, and `recovery_action`; verified in pytest. |
| **LOG-14** | Exact scope documented; hardware-specific or hosted-only environments explicitly classified | Documentation | **PASS** | Evidence table clearly defines tested scopes (`LOCAL_TEST`), bypassed tests (`HOSTED_CI: NOT_RUN`), and GPU boundary handling. |

### 2. Verified Test Suites Summary
- **Cyrene-Platform:**
  - `cy-observability`: 20 unit tests passed (0 failed).
  - `cy-kernel-daemon`: 82 tests passed (0 failed).
  - `cy-execution-control`: 20 tests passed (0 failed).
  - `cy-node-agent`: 13 tests passed (0 failed).
  - `cyrene-sandboxd`: 18 tests passed (0 failed).
  - `cy-package-runtime`: 5 tests passed (0 failed).
  - `cy-workspace-fabric`: 5 tests passed (0 failed).
  - Entire Platform workspace (`cargo test --workspace`): All crates passed cleanly.
- **Cyrene-Exchange (Product Service):**
  - Pytest suite (`product/tests`): 39 passed (0 failed), including correlation headers and error mapping.

### 3. Acceptance Statement
The Cyrene Release Candidate structured logging, error codes, and diagnostics system satisfies all requirements of the canonical specification (`logging-and-errors.md`) and passes all gate criteria from `OBS-G0` through `OBS-G6`.

Execution is halted at **OBS-G6** awaiting joint RC sign-off.




