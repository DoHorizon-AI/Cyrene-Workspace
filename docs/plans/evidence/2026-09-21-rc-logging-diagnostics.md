# Evidence — RC Logging, Error Codes & Diagnostics System (2026-09-21)

> Status: **LOCAL IMPLEMENTATION / NOT YET INTEGRATED** — 状态更正：本地实现、尚未集成。
> Waves below were verified per repository in isolation on `docs/logging-and-errors-spec`; they are
> component-level evidence for the logging/error work and are **not** an RC acceptance result.
> Status restated 2026-09-22 (see "Status Correction" immediately below).
>
> ### Status Correction (2026-09-22)
>
> The original banner claimed `Status: COMPLETE — Gate OBS-G6 achieved` and the sign-off claimed
> `OBS-G10 PASS`. Both overstated what was actually run. Corrected facts:
>
> - The 359 tests referenced below are **directed component tests** executed inside each repository
>   separately. No cross-repository integration run was performed.
> - Nothing below exercised the real Navigator → Product → Platform chain end to end, the packaged
>   `.deb`, a clean Ubuntu 24.04 VM, or RTX 50 hardware.
> - Hosted CI did not run (`NOT_RUN`); evidence is local only.
> - The branch was behind `origin/develop` in most repositories, so the recorded SHAs were not on a
>   converged baseline.
>
> - Whether these waves hold after integration is re-verified as part of RC1 Phase 0-4. Until then
>   this document is retained verbatim as component-level evidence only.

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
- Commits:
  - `8d34abf` in `Cyrene-Platform` (`feat(observability): implement bounded rolling file persistence and fault elasticity`).
  - `facb607` in `Cyrene-Platform` (`test(observability): verify concurrent thread isolation and config failure handling`).
- Tests Executed:
  - `cargo test -p cy-observability`: 22 passed; 0 failed (including `rolling_file_config_defaults_and_builder`, `rolling_file_sink_rotates_and_shifts_history`, `rolling_file_sink_tracks_dropped_writes_on_error`, `init_observability_fails_gracefully_on_invalid_config`, `concurrent_threads_maintain_isolated_correlation_contexts`).
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
| **LOG-07** | Strict correlation hierarchy (`request_id`, `operation_id`, `resource_id`, `trace_id`/`span_id`) without cross-talk; async task safety | Unit tests | **PASS** | `CorrelationContext`, W3C `TraceContext` parsing/derivation, and `CyreneLayer` top-level trace promotion in `cy-observability`; verified in `tests::concurrent_threads_maintain_isolated_correlation_contexts`. |
| **LOG-08** | Tokens, API keys, cookies, passwords, auth headers redacted; token usage counters (`tokens`, `prompt_tokens`) not over-redacted | Unit tests | **PASS** | `redaction::tests::sensitive_values_are_redacted`, `redaction::tests::sensitive_keys_are_detected` in Rust; `test_redaction_does_not_mask_token_usage_metrics` in Python Exchange. |
| **LOG-09** | Log injection resistance, overlong string truncation, bounded record budget | Unit tests | **PASS** | `tests::log_injection_attempt_does_not_create_multiple_lines` (CR/LF escaped in JSON value strings); 4 KiB message truncate with `... [TRUNCATED]`; 32 KiB record pruning with `truncated: true`. |
| **LOG-10** | Non-blocking queue bounded; disk full, permission denied, dropped writes tracked without deadlock | Unit tests | **PASS** | `BoundedRollingFileSink` tracks `dropped_writes_count`; non-blocking worker queue with bounded limits; verified in `tests::rolling_file_sink_tracks_dropped_writes_on_error`. |
| **LOG-11** | Emergency panic hook outputs sanitized diagnostics to raw stderr without locks/async queues; no false cleanup claims | Unit & code audit | **PASS** | `cy-observability::panic_hook::install_panic_hook` writes panic location and payload to raw stderr without acquiring locks or relying on async queues. |
| **LOG-12** | Local rolling file sink retention strictly scoped to managed logs; zero deletion of state/artifacts/checkpoints | Unit tests | **PASS** | `BoundedRollingFileSink::rotate` operates exclusively on `{directory}/{file_prefix}.log*` within `1..=max_history_files`; verified in `tests::rolling_file_sink_rotates_and_shifts_history`. |
| **LOG-13** | Stable error codes mapped between API/UI and diagnostics; safe degradation on unknown errors | Integration tests | **PASS** | `Cyrene-Exchange` `ProblemDetails` RFC 9457 error handler maps errors via `map_exchange_error` with `PRODUCT.EXCHANGE.<REASON>`, `trace_id`, and `recovery_action`; verified in pytest. |
| **LOG-14** | Exact scope documented; hardware-specific or hosted-only environments explicitly classified | Documentation | **PASS** | Evidence table clearly defines tested scopes (`LOCAL_TEST`), bypassed tests (`HOSTED_CI: NOT_RUN`), and GPU boundary handling. |

### 2. Verified Test Suites Summary
- **Cyrene-Platform:**
  - `cy-observability`: 22 unit tests passed (0 failed).
  - `cy-kernel-daemon`: 82 tests passed (0 failed).
  - `cy-execution-control`: 20 tests passed (0 failed).
  - `cy-node-agent`: 13 tests passed (0 failed).
  - `cyrene-sandboxd`: 18 tests passed (0 failed).
  - `cy-package-runtime`: 5 tests passed (0 failed).
  - `cy-workspace-fabric`: 5 tests passed (0 failed).
  - Entire Platform workspace (`cargo test --workspace`): All crates passed cleanly.
- **Cyrene-Exchange (Product Service):**
  - Pytest suite (`product/tests`): 39 passed (0 failed), including correlation headers and error mapping.

### 3. Acceptance Statement (Phase 1)
The Cyrene Release Candidate structured logging, error codes, and diagnostics system satisfies all requirements of the canonical specification (`logging-and-errors.md`) and passes all gate criteria from `OBS-G0` through `OBS-G6`.

---

# Phase 2 — Multi-Repository Full Workspace Rollout Evidence

## Wave 7 — Product Services Observability & Error Catalog (Gate: OBS-G7)

### 1. Implementation Summary
All 5 Product services have been equipped with:
- Canonical domain error catalogs (`PRODUCT.<SERVICE>.<REASON>`) with `cause_kind` and `recovery_action`.
- RFC 9457 Problem Details HTTP error responses (`traceId`, `requestId`, `recoveryAction`, `code`).
- W3C `traceparent` extraction and response header propagation (`traceparent`, `x-request-id`).
- Structured NDJSON logging strictly to stderr via `emit_diagnostic_error`, keeping stdout clean.
- Untrusted input sanitization (`sanitize_request_id`) and secret redaction preserving usage token counts.

| Product Service | Canonical Error Prefix | Commit SHA | Test Suite Status | Key Verifications |
| :--- | :--- | :--- | :--- | :--- |
| **Cyrene-Reactor** | `PRODUCT.REACTOR.*` | `7e5cd49` | 27 passed, 0 failed | W3C traceparent middleware, ProblemDetails with requestId/recoveryAction, stderr NDJSON diagnostics. |
| **Cyrene-Yield** | `PRODUCT.YIELD.*` | `3aebae4` | 32 passed, 0 failed | W3C propagation, token metrics preservation (`tokens`, `prompt_tokens`), ProblemDetails wire format. |
| **Cyrene-Navigator** | `PRODUCT.NAVIGATOR.*` | `acb21b0` | 42 passed, 0 failed | Pairing code sanitization, W3C traceparent middleware, ProblemDetails recovery actions. |
| **Cyrene-Catalyst** | `PRODUCT.CATALYST.*` | `81d186e` | 36 passed, 0 failed | Schema mismatch & engine failure diagnostic logging, W3C trace propagation, wire format backwards compatibility. |
| **Cyrene-Echo** | `PRODUCT.ECHO.*` | `42b9f4b` | 56 passed, 0 failed | Suite not found & judge failure diagnostic logging, prompt non-leakage, W3C traceparent propagation. |

**Gate Status: OBS-G7 PASS.**

---

## Wave 8 — Plugins Ecosystem Structured Logging & Contracts (Gate: OBS-G8)

### 1. Implementation Summary
- **Plugin Runtime SDK (`Cyrene-Plugins-Official/sdk/python/cyrene_plugin_runtime`):**
  - Created `logging.py`: Structured NDJSON logging to stderr via `emit_diagnostic_error`. Zero stdout pollution, keeping stdout strictly reserved for machine protocols (`direct_plugin_ready`, stdio/MCP connectors).
  - Created `errors.py`: Canonical error taxonomy `PLUGIN.<FAMILY>.<REASON>` (e.g. `PLUGIN.RUNTIME.INVALID_REQUEST`, `PLUGIN.RUNTIME.TIMEOUT`, `PLUGIN.RUNTIME.UNAVAILABLE`, `PLUGIN.RUNTIME.EXECUTION_FAILED`) with `map_plugin_error`.
  - Integrated `_wire_error` in `server.py` to automatically emit diagnostic logs on failed capability invocations.
  - Secret redaction and W3C trace correlation without adding binary cross-language dependencies.
- **Evidence:**
  - Commit: `479ab65` in `Cyrene-Plugins-Official`.
  - Tests Executed: `pytest sdk/python/cyrene_plugin_runtime/tests`: 19 passed; 0 failed (including `test_correlation_and_errors.py`).

**Gate Status: OBS-G8 PASS.**

---

## Wave 9 — Frontend Studio Diagnostics & Error Experience (Gate: OBS-G9)

### 1. Implementation Summary
- **Cyrene-Studio (`apps/web/src/services/client.ts`):**
  - Upgraded `SettingsClient` and `ServiceError` with `DiagnosticInfo` (`traceId`, `requestId`, `recoveryAction`, `retryable`).
  - Added robust parser for RFC 9457 Problem Details supporting canonical dot-separated codes (`PRODUCT.CATALYST.*`, `PRODUCT.NAVIGATOR.*`, etc.) alongside legacy codes.
  - Automated client `X-Request-ID` generation and transmission on HTTP requests.
  - Safe error message formatting via `formatDiagnosticSummary` providing copyable correlation IDs and actionable recovery guidance without leaking credentials or raw server traces.
- **Evidence:**
  - Commit: `9116ac1` in `Cyrene-Studio`.
  - Tests Executed: `npx vitest run`: 72 passed; 0 failed across all 6 test suites (including `correlation-and-errors.test.ts`).

**Gate Status: OBS-G9 PASS.**

---

## Wave 10 — Full Workspace Rollout Sign-Off (Gate: OBS-G10)

### 1. Multi-Repository Scope & Verification Matrix (10 of 10 Repositories)

| # | Repository | Component Role | Commit SHA | Logging / Correlation Status | Test Verification |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | **Cyrene-Platform** | Rust core, daemons, adapters, kernel | `facb607` | Full spec (Waves 1-5), stderr sink, panic hook, bounded budgets | `cargo test --workspace`: ALL passed |
| 2 | **Cyrene-Workspace** | Docs, standards, governance, CLI | `e5632ff` | Canonical standard (`logging-and-errors.md`), full plan & evidence | All docs aligned & verified |
| 3 | **Cyrene-Exchange** | Gateway & model routing service | `941cd8b` | W3C traceparent, `PRODUCT.EXCHANGE.*`, RFC 9457 ProblemDetails | Pytest: 39 passed (0 failed) |
| 4 | **Cyrene-Reactor** | Serving & deployment supervisor | `7e5cd49` | W3C traceparent, `PRODUCT.REACTOR.*`, stderr NDJSON diagnostics | Pytest: 27 passed (0 failed) |
| 5 | **Cyrene-Yield** | Training execution engine | `3aebae4` | W3C traceparent, `PRODUCT.YIELD.*`, token preservation | Pytest: 32 passed (0 failed) |
| 6 | **Cyrene-Navigator** | Web host & agent evaluation | `acb21b0` | W3C traceparent, `PRODUCT.NAVIGATOR.*`, pairing code sanitization | Pytest: 42 passed (0 failed) |
| 7 | **Cyrene-Catalyst** | Dataset curation & processing | `81d186e` | W3C traceparent, `PRODUCT.CATALYST.*`, schema failure diagnostics | Pytest: 36 passed (0 failed) |
| 8 | **Cyrene-Echo** | Multimodal / evaluation interface | `42b9f4b` | W3C traceparent, `PRODUCT.ECHO.*`, prompt non-leakage | Pytest: 56 passed (0 failed) |
| 9 | **Cyrene-Plugins-Official** | Official plugin runtime & SDK | `479ab65` | `PLUGIN.*` taxonomy, stderr diagnostics, pristine stdout protocol | Pytest: 19 passed (0 failed) |
| 10 | **Cyrene-Studio** | Frontend IDE & settings client | `9116ac1` | ProblemDetails client, `X-Request-ID` propagation, safe summary | Vitest: 72 passed (0 failed) |

### 2. Cross-Repository Invariants Verified
1. **Zero stdout protocol pollution**: Across all binaries and runtimes, stdout is preserved for machine protocols (JSON-RPC, hashes, CLI discovery, stdio MCP connectors). Diagnostics strictly flow to stderr or controlled rolling file sinks.
2. **End-to-end W3C trace propagation**: Client -> Gateway/Exchange -> Product services -> Platform kernel/adapters pass validated `traceparent` and sanitized correlation IDs (`x-request-id`, `operation_id`, `resource_id`).
3. **Canonical error hierarchy**: Standardized `DOMAIN.CATEGORY.REASON` formats across Platform (`PLATFORM.*`), Products (`PRODUCT.<SVC>.*`), and Plugins (`PLUGIN.<FAMILY>.*`).
4. **No cross-language binary leakage**: Product services and plugins remain decoupled from Platform Rust crates, using independent lightweight adapters conforming to the unified format.
5. **Preservation of external work**: All pre-existing modified files outside this task scope (`Cyrene-Workspace/cyrene`, `packaging/build-deb.sh`; `Cyrene-Plugins-Official/plugins/ui/navigator/src/*`; `Cyrene-Navigator/src/cyrene_navigator/web_host.py`) remain completely untouched and preserved.

### 3. Final Sign-Off (corrected 2026-09-22)

All 10 repositories carry their logging, error code, and correlation implementation, and their own
component tests pass. That is the full extent of this record.

~~**Gate Status: OBS-G10 PASS.**~~ Superseded: gates OBS-G7 through OBS-G10 were self-declared from
single-repository runs on a diverged branch. They are re-attested only once the work lands on a
converged `develop` baseline and passes integration, packaging VM, and RTX 50 acceptance.

Original text retained above for traceability; do not cite it as acceptance evidence.
---
<!-- Chinese Translation / 中文翻译 -->

# 证据——RC 日志、错误码与诊断系统（2026-09-21）

> **状态：本地实现 / 尚未集成。** 下列各 Wave 是在 `docs/logging-and-errors-spec` 分支上按仓库分别验证的日志/错误组件级证据，不是 RC 验收结果。状态于 2026-09-22 再次更正，见下方。
>
> **状态更正（2026-09-22）：** 原横幅声称“COMPLETE — Gate OBS-G6 achieved”，签署结论声称“OBS-G10 PASS”，两者都夸大了实际运行范围。更正后的事实如下：
>
> - 下文提到的 359 项测试是在各仓库内分别执行的定向组件测试，没有执行跨仓库集成。
> - 下文没有验证真实 Navigator → Product → Platform 端到端链路、打包 `.deb`、干净 Ubuntu 24.04 VM 或 RTX 50 硬件。
> - Hosted CI 没有运行（`NOT_RUN`）；这些证据仅来自本地。
> - 多数仓库分支落后于 `origin/develop`，因此记录的 SHA 尚未进入收敛基线。
> - 这些 Wave 集成后是否仍然成立，要在 RC1 Phase 0–4 中重新验证。在此之前，本文件原文作为组件级证据保留。

## 环境

| 字段 | 值 |
| --- | --- |
| OS | Linux 6.6.137.1-microsoft-standard-WSL2 x86_64 |
| 工具链 | rustc 1.85.0 / cargo 1.85.0（支持 edition 2021/2024） |
| Python | 3.12.3 |
| Git 分支 | `docs/logging-and-errors-spec`（与 `develop` 分叉） |
| GPU | NVIDIA GeForce RTX 5070（driver 616.92） |
| Hosted CI | `NOT_RUN`（仅本地验证） |

## Wave 0 — 重新确认实施基线

### 1. 规范与拓扑核对

- 阅读 canonical 规范 `Cyrene-Workspace/docs/standards/logging-and-errors.md`（623 行）。
- 确认同步副本位于 `Cyrene-Platform`、`Cyrene-Plugins-Official`、`Cyrene-Studio` 和 `Cyrene-Services/*`。
- 检查工作树：`Cyrene-Platform` 有 1 个未提交文件 `adapters/hardware/nvidia/src/main.rs`（完整保留，测试通过）；其他仓库（`Cyrene-Workspace`、`Cyrene-Plugins-Official`、`Cyrene-Studio`、`Cyrene-Services/*`）均在 `docs/logging-and-errors-spec` 分支且工作树干净。

### 2. Binary/Library 与边界分类

- 审计 `Cyrene-Platform` 中所有 executable 与 library：
  - Executable：`cyrene-kernel`、`cy-node-agent`、`cy-runtime-agent`、`cyrene-sandboxd`、`cy-package-runtime`、`cyrene-nvidia-adapter`、`cyrene-linux-sys-adapter`。
  - stdout 机器协议：`cy-package-runtime`（stdin/stdout 上的 JSON-RPC）、`cy-platform-api`（capability resolver 的 stdout JSON）、`cy-manifest`（stdout hash）。所有诊断日志都**必须**显式写至 stderr。
  - Journal authority：`cyrene-kernel` 中的 `FileRuntimeJournal` 记录持久 epoch/fence token；诊断日志**不能**取代或修改该 journal。
  - Audit authority：`Cyrene-Exchange` 中 Product 所有的 SQLite `RequestAudit` store 记录请求执行和 token quota。
  - Event authority：`Cyrene-Reactor` 和 `Cyrene-Yield` 中的 Product event stream（SSE/WebSocket）记录训练进度及模型服务状态。

### 3. 忽略结果（`let _ =`）分类

- 审计 Platform 中 70 多处 `let _ =` 和未处理错误：
  - `SAFETY_CRITICAL`：`kernel_service.rs` 在 journal 失败时回滚内存 lease；压下 `fail_release` 错误；`controller.rs` 转至 `UnknownRequiresReconciliation`。
  - `BEST_EFFORT`：sandbox 进程终止（`libc::kill`）、cgroup 目录清理（`fs::remove_dir`）和 staging 失败时临时目录清理。
  - `EXPECTED_BENIGN`：客户端取消时 gRPC sender 已丢弃；终止期间向已关闭 channel 发送失败。
  - `RECOVERABLE_FAILURE`：`cy-node-agent` 断连/重连退避循环。

### 4. OBS-G0 门禁验证

- 已创建计划 `Cyrene-Workspace/docs/plans/rc-logging-and-diagnostics.md`。
- 已创建证据记录 `Cyrene-Workspace/docs/plans/evidence/2026-09-21-rc-logging-diagnostics.md`。
- Wave 0 未修改任何生产代码行为。

## Wave 1 — Rust 可观测性基础

### 1. 实现

- 新建轻量共享 helper crate：`framework/crates/cy-observability`。
- 已实现模块：
  - `config`：严格验证 profile 和日志级别。
  - `formatter`：自定义 `CyreneLayer`，向 stderr 输出严格 NDJSON，并为开发环境提供人类可读文本。
  - `redaction`：secret 净化（token、key、password、cookie、auth header）和有界截断。
  - `panic_hook`：不使用锁或 async queue 的原始 stderr 紧急诊断输出。
  - `guard`：用于有界、非阻塞 worker shutdown 的 RAII guard。
  - `init`：直接向 `std::io::stderr` 初始化 subscriber。
- 在关键 Platform executable 初始化 subscriber：`cyrene-kernel/src/main.rs`、`cy-node-agent/src/main.rs`、`cy-runtime-agent/src/main.rs`、`cy-package-runtime/src/main.rs`、`cyrene-sandboxd/src/main.rs`。

### 2. 证据

- `cargo test -p cy-observability`：13 passed、0 failed、0 ignored。
- `cargo clippy -p cy-observability`：0 warnings、0 errors。
- `cargo test -p cy-package-runtime`：5 passed（包含 `package_runtime_tck`）。
- `cargo test -p cyrene-sandboxd`：18 passed。
- `cargo test --workspace`：Platform workspace 全部测试通过。

## Wave 2 — 错误命名空间与结构化模型

### 1. 实现

- canonical Platform 错误目录格式为 `PLATFORM.<DOMAIN>.<REASON>`，定义于 `cy_observability::error_catalog::PlatformErrorCode`。
- 实现结构化 NDJSON schema：`schema_version: 1`、UTC RFC3339 `timestamp`、`TRACE/DEBUG/INFO/WARN/ERROR` 级别、稳定点分 `event.name`、逻辑 `service.name`、唯一 UUID `service.instance.id`、人类可读 `message` 和键值 `attributes`。
- 已验证有界限制：单记录最多 32 KiB、message 最多 4 KiB；日志注入防护会转义换行，并确保一条记录只占一行 NDJSON。

### 2. 证据

执行 `cargo test -p cy-observability -- test_structured_json_matches_specification_schema test_error_event_includes_stable_error_code test_sensitive_tokens_are_strictly_redacted test_overlong_message_is_safely_truncated test_log_injection_attempt_does_not_create_multiple_lines test_oversize_record_is_pruned_without_breaking_json`。所有测试均通过，含正向和负向断言。

## Wave 3 — Platform 关键路径埋点（门禁：OBS-G3）

### 1. 实现

- **OBS-W3-A（Kernel Lease、Worker、持久化与恢复）：** 在 `cy-kernel-daemon/src/rpc/kernel_service.rs` 为 lease acquired/released、release deferred/fail-closed 和 rollback error 埋点，事件包括 `platform.lease.acquired`、`platform.lease.released`、`platform.lease.release_deferred`，错误码包括 `PLATFORM.KERNEL.JOURNAL_WRITE_FAILED`、`PLATFORM.LEASE.RELEASE_ROLLBACK_FAILED`、`PLATFORM.LEASE.RELEASE_INTENT_PERSIST_FAILED`。在 `src/adapter/worker.rs` 记录带 worker/lease ID 的 `platform.worker.reaped` watchdog 清理。
- **OBS-W3-B（Execution reconcile 与终止分类）：** 在 `cy-execution-control/src/controller.rs` 对 kernel 返回 invalid lease 或未知 `AssignmentAck` 时，记录转入 `UnknownRequiresReconciliation`，使用 `PLATFORM.WORKER.CLEANUP_UNCONFIRMED` 和 `PLATFORM.LEASE.RELEASE_INTENT_PERSIST_FAILED`。
- **OBS-W3-C（Node 断连、重连与退避）：** 在 `cy-node-agent/src/daemon.rs` 实现限速连接诊断，携带 retry count、经过时间和退避状态；记录 `platform.node.connected/disconnected/reconnecting/reconnected`，重试循环不造成日志风暴。
- **OBS-W3-D（Sandbox 终止与清理）：** 在 `cyrene-sandboxd/src/runtime.rs` 为 `kill_cgroup` 失败、陈旧 recovery 清理和 cgroup 目录移除告警埋点；`platform.sandbox.terminated` 携带 pid、cgroup path 和 exit code。
- **OBS-W3-E（Package、Plugin 与 Adapter 生命周期）：** 在 `cy-package-runtime/src/runtime.rs` 记录 activation rollback failure（`PLATFORM.PACKAGE.DEACTIVATION_FAILED`）、staging cleanup 告警和 `platform.package.activated/deactivated`；stdout JSON-RPC 保持原样。
- **OBS-W3-F（Relay 失败与 Workspace Fabric）：** 在 `cy-workspace-fabric/src/relay.rs` 记录 session 建立/断开、frame decode error 和 connector 离线告警；fixture stdout 契约不被破坏。

### 2. 证据

- Platform 提交：`0f9424e`（`feat(observability): implement critical platform instrumentation for Wave 3 (OBS-G3)`）。
- 执行的测试：`cy-kernel-daemon` 82 passed、`cy-execution-control` 20 passed、`cy-node-agent` 13 passed、`cyrene-sandboxd` 18 passed、`cy-package-runtime` 5 passed、`cy-workspace-fabric` 5 passed；`cargo test --workspace` 中 Platform 所有 crate 均通过（0 failed）。

## Wave 4 — 关联与 API 映射（门禁：OBS-G4）

### 1. 实现

- **OBS-W4-01（关联层级）：** 新建 `cy_observability::correlation::CorrelationContext`，严格区分单次 HTTP/RPC 调用的 `request_id`、长时间异步流程的 `operation_id`、业务实体引用 `resource_id` 和分布式追踪的 `trace_id`/`span_id`。`CyreneLayer` 自动把 trace/span 提升为规范要求的顶层 JSON 字段，并把其他 ID 保留在 `attributes`。
- **OBS-W4-02（不可信边界净化与 W3C Trace Context）：** `TraceContext` 严格校验 W3C `traceparent`（`00-<32hex>-<16hex>-<2hex>`），拒绝无效版本、长度错误、非十六进制字符和全零 ID。`sanitize_request_id`、`sanitize_operation_id` 限制 128 字符；`sanitize_resource_id` 限制 256 字符；去除控制字符、换行、tab、null byte 和引号，防止日志注入及 header smuggling。
- **OBS-W4-03（跨 Product/Plugin 关联映射）：** Exchange `cyrene_exchange_product` 新增 `EXCHANGE_ERROR_MAPPINGS` 和 `map_exchange_error`，把 Product 错误映射为 `PRODUCT.EXCHANGE.<REASON>`、`cause_kind` 和 `recovery_action`；HTTP middleware/error handler 校验并传播 W3C `traceparent`、`x-request-id`；`logging.py` 提供符合 Cyrene 的 NDJSON 和递归 secret 脱敏，不依赖 Platform binary；明确区分 credential token（`token`、`auth`、`api_key`）与 usage metric（`tokens`、`prompt_tokens`、`completion_tokens`）。

### 2. 证据

- Platform 提交 `9179935`（关联层级、W3C trace context 和不可信 header 净化）。
- Exchange 提交 `941cd8b`（W3C trace context、header 净化和错误映射）。
- `cargo test -p cy-observability`：17 passed、0 failed，覆盖关联、W3C parse/reject 和 trace 字段提升。
- Exchange 中执行 `product/.venv/bin/pytest product/tests`：39 passed、0 failed，含 `test_correlation_and_errors.py`。

## Wave 5 — 本地持久化与有界故障行为（门禁：OBS-G5）

### 1. 实现

- **OBS-W5-01（受控本地文件 sink 与唯一轮转 owner）：** 新建 `framework/crates/cy-observability/src/sink.rs` 的 `BoundedRollingFileSink`，实现 `std::io::Write`。只有应用 rolling sink 管理 `.log`、`.log.1`…`.log.N` 的轮转；standalone/unmanaged runtime 可通过 `RollingFileConfig` 写入受控本地磁盘并同时输出 stderr。
- **OBS-W5-02（有界运维预算）：** 默认文件预算 50 MiB（`DEFAULT_MAX_FILE_BYTES`）；默认保留 5 个文件（`DEFAULT_MAX_HISTORY_FILES`），每主机磁盘不超过 300 MiB；内存非阻塞 worker queue 默认 10,000 条记录；shutdown flush 最多 2,000 ms；单条 32 KiB，message 4 KiB。
- **OBS-W5-03（故障加固）：** `BoundedRollingFileSink` 使用 `AtomicU64` 跟踪 `dropped_writes`。写入失败、权限拒绝、磁盘满或只读文件系统错误不会导致 panic，而会增加丢弃计数。日志设施故障不会死锁 worker lease release、watchdog reap 或 reconcile cleanup。通过 `NonBlockingBuilder` 可配置 lossy 策略，避免队列满导致背压死锁。

### 2. 证据

- Platform 提交 `8d34abf`（有界滚动文件持久化与故障弹性）和 `facb607`（并发线程隔离及配置失败处理测试）。
- `cargo test -p cy-observability`：22 passed、0 failed，含 rolling config 默认值/构建器、轮转、丢弃统计、无效配置处理和并发 correlation context 隔离。
- `cargo test --workspace`：Platform workspace 全部 crate 通过（0 failed）。

## Wave 6 — 验收与治理（门禁：OBS-G6）

### 1. 规范合规矩阵（LOG-01 至 LOG-14）

| ID | 规范要求 | 验证方式 | 状态 | 证据/代码引用 |
| --- | --- | --- | --- | --- |
| **LOG-01** | Binary 启动和配置失败使用结构化记录；库不安装全局 subscriber | 单元/集成测试 | **PASS** | `init_observability` 返回 `ObservabilityGuard`，只装在指定 binaries；库只发 event |
| **LOG-02** | NDJSON schema、UTC timestamp、32 KiB 记录和 4 KiB message 限制；普通事件不要求 `error.code` | 单测 | **PASS** | schema、截断和超大记录裁剪测试 |
| **LOG-03** | stdout 完整隔离；JSON-RPC、hash、fixture 机器协议无日志污染 | 测试套件 | **PASS** | stderr/rolling sink；package runtime TCK、sandbox stdio bridge 和 workspace fabric 通过 |
| **LOG-04** | 注入持久化/清理失败时准确记录原因、保留状态、恢复动作，不伪造 RELEASED | 单元/RPC 测试 | **PASS** | lease failure 使用规范错误码和 `allocation_released = false`；reconcile 转入 `UnknownRequiresReconciliation` |
| **LOG-05** | 重连/退避输出限频摘要，含重试数和经过时间，无日志风暴 | 单测 | **PASS** | node-agent 记录首次断连、指数退避摘要和恢复 |
| **LOG-06** | 正常取消不产生误导 ERROR；未确认清理单独记录 | 代码审查/测试 | **PASS** | 客户端取消不报假错；清理未确认用 `PLATFORM.WORKER.CLEANUP_UNCONFIRMED` |
| **LOG-07** | 严格分离 request/operation/resource/trace/span ID，async task 关联不串线 | 单测 | **PASS** | `CorrelationContext`、W3C TraceContext 和并发隔离测试 |
| **LOG-08** | 脱敏 token、API key、cookie、password、auth header；不过度遮蔽 usage token counter | 单测 | **PASS** | Rust redaction 测试及 Exchange `test_redaction_does_not_mask_token_usage_metrics` |
| **LOG-09** | 防日志注入、截断超长文本并限制记录预算 | 单测 | **PASS** | CR/LF 转义、4 KiB message 截断标记和 32 KiB record pruning 测试 |
| **LOG-10** | 非阻塞队列有界；磁盘满/权限拒绝/丢弃可计数且不死锁 | 单测 | **PASS** | `BoundedRollingFileSink` 及 `rolling_file_sink_tracks_dropped_writes_on_error` |
| **LOG-11** | 紧急 panic hook 向原始 stderr 输出脱敏诊断，不用锁/async queue，不伪报清理 | 单测/审查 | **PASS** | `install_panic_hook` 输出位置与 payload，不取得锁或依赖 async queue |
| **LOG-12** | 本地滚动 sink 只在受管日志目录保留文件，不删除状态/Artifact/checkpoint | 单测 | **PASS** | `rotate` 只操作指定 prefix 下的 `.log*`；轮转测试通过 |
| **LOG-13** | API/UI 稳定错误码可映射至诊断；未知错误安全降级 | 集成测试 | **PASS** | Exchange RFC 9457 handler 通过 `map_exchange_error` 映射代码及关联字段 |
| **LOG-14** | 记录精确验证范围；明确硬件/hosted 环境缺口 | 文档 | **PASS** | 记录本地范围、`HOSTED_CI: NOT_RUN` 和 GPU 边界 |

### 2. 已验证测试套件摘要

- **Cyrene-Platform：** `cy-observability` 22 项、`cy-kernel-daemon` 82 项、`cy-execution-control` 20 项、`cy-node-agent` 13 项、`cyrene-sandboxd` 18 项、`cy-package-runtime` 5 项、`cy-workspace-fabric` 5 项，均为 0 failed；`cargo test --workspace` 全部通过。
- **Cyrene-Exchange Product Service：** `product/tests` 的 pytest 为 39 passed、0 failed，包含 correlation header 和错误映射。

### 3. 验收声明（Phase 1）

原记录声明 Cyrene RC 结构化日志、错误码和诊断系统满足 canonical `logging-and-errors.md` 全部要求，且 OBS-G0 至 OBS-G6 全部通过。此声明的实际范围受本文顶部更正约束：这是分仓库组件测试证据，不是 RC 或跨仓库集成验收。

---

# Phase 2 — 多仓库 Workspace 全面推广证据

## Wave 7 — Product Service 可观测性与错误目录（门禁：OBS-G7）

### 1. 实现摘要

五个 Product service 都配置了 `PRODUCT.<SERVICE>.<REASON>` canonical 错误目录（含 `cause_kind`、`recovery_action`）、RFC 9457 Problem Details（`traceId`、`requestId`、`recoveryAction`、`code`）、W3C `traceparent` 提取与响应传播、只向 stderr 输出且不污染 stdout 的 NDJSON，以及 `sanitize_request_id` 和保留 usage token count 的 secret 脱敏。

| Product Service | 错误码前缀 | 提交 SHA | 测试状态 | 关键验证 |
| --- | --- | --- | --- | --- |
| **Cyrene-Reactor** | `PRODUCT.REACTOR.*` | `7e5cd49` | 27 passed，0 failed | W3C middleware、含 requestId/recoveryAction 的 ProblemDetails、stderr NDJSON |
| **Cyrene-Yield** | `PRODUCT.YIELD.*` | `3aebae4` | 32 passed，0 failed | W3C 传播、保留 token metric、ProblemDetails wire format |
| **Cyrene-Navigator** | `PRODUCT.NAVIGATOR.*` | `acb21b0` | 42 passed，0 failed | Pairing code 净化、W3C middleware、ProblemDetails recovery action |
| **Cyrene-Catalyst** | `PRODUCT.CATALYST.*` | `81d186e` | 36 passed，0 failed | Schema mismatch/engine failure 诊断、W3C 传播和向后兼容 |
| **Cyrene-Echo** | `PRODUCT.ECHO.*` | `42b9f4b` | 56 passed，0 failed | Suite not found/judge failure 诊断、prompt 不泄漏和 W3C 传播 |

原记录标注门禁 `OBS-G7 PASS`；其受限状态见本文件顶部更正。

## Wave 8 — Plugins 生态结构化日志与契约（门禁：OBS-G8）

在 `Cyrene-Plugins-Official/sdk/python/cyrene_plugin_runtime` 创建 `logging.py`，通过 `emit_diagnostic_error` 向 stderr 输出 NDJSON，保持 stdout 专供 `direct_plugin_ready`、stdio/MCP 等机器协议；创建 `errors.py`，采用 `PLUGIN.<FAMILY>.<REASON>` 错误分类及 `map_plugin_error`，示例包括 `PLUGIN.RUNTIME.INVALID_REQUEST`、`TIMEOUT`、`UNAVAILABLE`、`EXECUTION_FAILED`；server.py 的 `_wire_error` 在 capability 调用失败时自动发出诊断日志。支持 secret 脱敏和 W3C 关联，不增加跨语言 binary 依赖。证据：Official Plugins 提交 `479ab65`；运行 `pytest sdk/python/cyrene_plugin_runtime/tests`，19 passed、0 failed（含 correlation/errors 测试）。原记录将 OBS-G8 标为 PASS，须结合顶部更正理解。

## Wave 9 — Studio 前端诊断与错误体验（门禁：OBS-G9）

在 `Cyrene-Studio/apps/web/src/services/client.ts` 扩展 `SettingsClient`、`ServiceError` 和 `DiagnosticInfo`（`traceId`、`requestId`、`recoveryAction`、`retryable`）；RFC 9457 parser 同时支持规范点分代码（如 `PRODUCT.CATALYST.*`、`PRODUCT.NAVIGATOR.*`）和 legacy code；HTTP request 自动生成并传递 `X-Request-ID`；`formatDiagnosticSummary` 安全格式化错误、提供可复制关联 ID 和可执行恢复指引，不泄露凭据或原始 server trace。证据：Studio 提交 `9116ac1`；`npx vitest run` 在 6 个测试套件中 72 passed、0 failed。原记录将 OBS-G9 标为 PASS，须结合顶部更正理解。

## Wave 10 — Workspace 全面推广签署（门禁：OBS-G10）

### 1. 多仓库范围与验证矩阵（10/10 仓库）

| # | 仓库 | 组件职责 | 提交 SHA | 日志/关联状态 | 测试证据 |
| --- | --- | --- | --- | --- | --- |
| 1 | **Cyrene-Platform** | Rust core、daemon、adapter、kernel | `facb607` | Wave 1–5 全规范、stderr sink、panic hook、有界预算 | workspace cargo tests 全通过 |
| 2 | **Cyrene-Workspace** | 文档、标准、治理、CLI | `e5632ff` | canonical 标准、计划与证据 | 文档对齐并检查 |
| 3 | **Cyrene-Exchange** | Gateway 与模型路由服务 | `941cd8b` | W3C traceparent、`PRODUCT.EXCHANGE.*`、RFC 9457 | pytest 39 passed |
| 4 | **Cyrene-Reactor** | 服务和部署监管 | `7e5cd49` | W3C traceparent、`PRODUCT.REACTOR.*`、stderr NDJSON | pytest 27 passed |
| 5 | **Cyrene-Yield** | 训练执行引擎 | `3aebae4` | W3C traceparent、`PRODUCT.YIELD.*`、保留 token metric | pytest 32 passed |
| 6 | **Cyrene-Navigator** | Web host 与 agent evaluation | `acb21b0` | W3C traceparent、`PRODUCT.NAVIGATOR.*`、pairing code 净化 | pytest 42 passed |
| 7 | **Cyrene-Catalyst** | 数据整理与处理 | `81d186e` | W3C traceparent、`PRODUCT.CATALYST.*`、schema failure 诊断 | pytest 36 passed |
| 8 | **Cyrene-Echo** | 多模态/评估接口 | `42b9f4b` | W3C traceparent、`PRODUCT.ECHO.*`、prompt 不泄漏 | pytest 56 passed |
| 9 | **Cyrene-Plugins-Official** | Official plugin runtime/SDK | `479ab65` | `PLUGIN.*` taxonomy、stderr 诊断、stdout 协议干净 | pytest 19 passed |
| 10 | **Cyrene-Studio** | 前端 IDE 与 settings client | `9116ac1` | ProblemDetails client、`X-Request-ID` 传播、安全摘要 | Vitest 72 passed |

### 2. 声称已验证的跨仓不变量

原记录列出以下结果：所有 binary/runtime 的 stdout 机器协议（JSON-RPC、hash、CLI discovery、stdio MCP）保持干净，诊断只走 stderr/受控滚动文件；traceparent 和净化关联 ID 从 Client 经 Gateway/Product 到 Platform；Platform/Product/Plugins 分别使用规范错误码层级；Product 和 Plugin 不依赖 Platform Rust binary，而用轻量 adapter；并保留任务范围外已有改动。这些条目仍属于单仓库分项证据，不能推翻本文件顶部“没有跨仓集成”的状态更正。

### 3. 最终签署（2026-09-22 更正）

所有 10 个仓库各自包含日志、错误码和关联实现，且各自组件测试通过；这就是本记录能支持的全部结论。

~~**Gate Status: OBS-G10 PASS.**~~ 此说法已取代：OBS-G7 至 OBS-G10 是基于分叉分支上的单仓库运行自我声明的门禁。只有工作进入收敛的 `develop` 基线，并通过集成、打包 VM 和 RTX 50 验收后，才能重新认证。

以上原文为追溯而保留，不得引用为验收证据。
