# Cyrene RC 日志、错误码与诊断系统实施计划 / RC Logging and Diagnostics Plan

> **中文主计划 / Primary plan:** 落实 Workspace 已接受的 `logging-and-errors` 规范，建立 Cyrene RC 的结构化日志、错误码命名空间、关联诊断、本地持久化与容灾预算。以 Platform Rust 关键运行路径为第一实施重点，建立跨 Product 的最小 correlation 与 error mapping。
>
> **English summary:** Implementation plan for Cyrene RC logging, error code namespace, correlation, local bounded persistence, and diagnostics system, adhering strictly to the accepted `logging-and-errors.md` specification. Focuses on Platform Rust critical execution paths and cross-product correlation.

---

## 0. Document control / 文档控制

| Field | Value |
| --- | --- |
| Plan ID | `CYRENE-RC-LOGGING-DIAGNOSTICS-V1` |
| Current status | IMPLEMENTING — Wave 0 completed with baseline matrices and evidence; Wave 1-6 planned |
| Specification | [`docs/standards/logging-and-errors.md`](../standards/logging-and-errors.md) |
| Evidence record | [`evidence/2026-09-21-rc-logging-diagnostics.md`](evidence/2026-09-21-rc-logging-diagnostics.md) |
| Core focus | Platform Rust critical daemons/binaries, cross-layer correlation, bounded resilience |
| Explicit exclusions | No new telemetry SaaS/daemon, no OTLP collector dependency for RC, no state-machine rewrite, no substituting logs for journals/audits |

---

## 1. Status convention / 状态约定

- `[ ]` 表示该项检查尚未完成或未获得实际环境充分证据。
- `[x]` 表示该项检查已完成且获得实测证据（代码落地、单元/集成测试通过、证据记录闭环）。
- 任何无法在当前环境（如需无头物理多机/真实英伟达硬隔离环境）完全闭环的项，明确标注环境限制（如 `LOCAL_TEST_PASS`），不虚标 `ACTUAL_PASS`。

---

## 2. Component Coverage Matrix / 组件覆盖矩阵

| Component / Crate | Type | Diagnostic Role | stdout Machine Protocol | Journal / Audit / Event Boundary |
| --- | --- | --- | --- | --- |
| `runtime/cyrene-kernel` | Executable Binary | Composition root for Kernel daemon; joins adapters, sandbox, recovery | NONE (all diagnostics to stderr) | Reads/writes `FileRuntimeJournal` (epoch & fence crash recovery); never replaces journal |
| `kernel/crates/cy-kernel-daemon` | Library | Core lease, allocation, worker lifecycle, watchdog | NONE | Issues `RuntimeJournalEvent` to `RuntimeJournalSink`; authoritative lease & fence manager |
| `kernel/crates/cy-resource-manager` | Library | In-memory resource and fence tracking | NONE | Authoritative in-memory state; no logging I/O under lock |
| `kernel/crates/cy-sandbox-client` | Library | Client for out-of-process sandboxd | NONE | IPC frames over UDS; failure must not fabricate worker state |
| `adapters/execution/sandboxd` | Executable & Lib | Privileged cgroup v2 & process reaper | NONE (all diagnostics to stderr) | Executes kernel termination directives; independent process tree reaper |
| `adapters/hardware/nvidia` | Executable & Lib | NVIDIA hardware discovery & UDS adapter | NONE (all diagnostics to stderr) | Hardware inventory facts; peer credential enforcement |
| `adapters/hardware/linux_sys` | Executable & Lib | Linux system & NUMA discovery adapter | NONE (all diagnostics to stderr) | Host system facts |
| `agents/node/cy-node-agent` | Executable & Lib | Outbound mTLS control plane connector & backoff | NONE (all diagnostics to stderr) | Local session & upgrade journal (`NodeAgentJournal`); backoff retry |
| `agents/runtime/cy-runtime-agent` | Executable & Lib | Container-only runtime agent execution | NONE (all diagnostics to stderr) | Container isolation; unprivileged worker execution |
| `framework/crates/cy-package-runtime` | Executable & Lib | Package lifecycle, worker activation | **CRITICAL: JSON-RPC over stdin/stdout** | stdio protocol MUST remain unpolluted; diagnostics to stderr |
| `framework/crates/cy-execution-control` | Library | Dispatch controller, intent store, session | NONE | Durable intent store (`FileIntentStore`); authoritative state transition |
| `framework/crates/cy-execution-fabric` | Library | Pure reconciliation, placement, admission | NONE | Pure functions; callers record decisions at boundary |
| `framework/crates/cy-workspace-fabric` | Library & Fixture | Workspace relay connectivity & discovery | Fixture stdout protocol | Relay stream framing; error frame mapping |
| `framework/crates/cy-platform-api` | Library & CLI | Capability resolver | **CRITICAL: JSON output on stdout** | Output JSON to stdout; diagnostics to stderr |
| `contracts/rust/cy-manifest` | Library & CLI | Manifest parser & hash CLI | **CRITICAL: Hash ID on stdout** | CLI stdout for pipeline piping; diagnostics to stderr |
| `Cyrene-Services/Cyrene-Exchange` | Python Product | API gateway, API key management, route dispatch | HTTP API | SQLite `RequestAudit` store is terminal authority; W3C Trace Context |
| `Cyrene-Services/Cyrene-Reactor` | Python Product | Model import & serving lifecycle supervisor | HTTP API | Persistent ModelImport & Deployment state; Product SSE events |
| `Cyrene-Services/Cyrene-Catalyst` | Python Product | Dataset curation & processing | HTTP API | Immutable DatasetVersion & quality report |

---

## 3. Ignored Result Classification Matrix / 忽略结果分类矩阵

| Location | Expression | Category | Reason / Handling Strategy |
| --- | --- | --- | --- |
| `kernel_service.rs:95-96` | `let _ = self.daemon.begin_release...` | `SAFETY_CRITICAL` | Rollback of in-memory lease on journal write failure; failure to rollback leaves allocation leaked. Must log ERROR with `PLATFORM.LEASE.RELEASE_ROLLBACK_FAILED` and retain unreleased allocation. |
| `kernel_service.rs:408,420,457,466` | `let _ = self.daemon.fail_release...` | `SAFETY_CRITICAL` | Quarantine/failed lease release state transition. Must log ERROR if fail_release returns Err. |
| `kernel_service.rs:432,590` | `eprintln!("runtime journal write failed: {error}")` | `SAFETY_CRITICAL` | Telemetry journal record failed; currently ad-hoc eprintln. Must emit structured ERROR `PLATFORM.KERNEL.JOURNAL_WRITE_FAILED`. |
| `worker.rs:474,529,586,629,662,695,714` | `eprintln!("worker lost authority...")` | `SAFETY_CRITICAL` / `RECOVERABLE_FAILURE` | Watchdog background reap and lease expiry. Must emit structured WARN/ERROR with operation context. |
| `controller.rs:228,407` | `let _ = self.transition_intent(..., UnknownRequiresReconciliation, ...)` | `SAFETY_CRITICAL` | Intent store failure during transition to reconciliation. Failure to persist must emit structured ERROR. |
| `sandboxd/src/runtime.rs:821,837,891,1054` | `let _ = libc::kill(...)` / `let _ = self.kill_cgroup(...)` | `BEST_EFFORT` | Process tree killing during teardown; process may already have exited (ESRCH). Benign if exited, log DEBUG if unexpected errno. |
| `sandboxd/src/runtime.rs:154,414,892,928,1016` | `let _ = fs::remove_file/dir(...)` | `BEST_EFFORT` | Cgroup or socket cleanup; if directory already removed or absent, benign. Log TRACE/DEBUG on failure. |
| `cy-package-runtime/src/runtime.rs:522,654,1027` | `let _ = fs::remove_dir_all(&stage)` | `BEST_EFFORT` | Staging dir cleanup after package failure; failure to delete temp dir does not compromise runtime integrity. |
| `kernel_service.rs:697` / `authority_v2_service.rs:357-421` | `let _ = sender.send(...)` | `EXPECTED_BENIGN` | Client disconnected before response/error could be written across gRPC stream. Expected when client cancels early. |
| `cy-node-agent/src/daemon.rs:136,164` | `Err(_) => { sleep(delay); backoff... }` | `RECOVERABLE_FAILURE` | Node agent connection loss. Must record initial failure, rate-limited summary, and recovery without flood. |
| `cyrene-nvidia-adapter/src/main.rs:178` | `let _ = writeln!(diagnostics, ...)` | `BEST_EFFORT` | Emitting diagnostics to caller-injected writer. |

---

## 4. Waves & Gates / 分阶段实施门禁

### Wave 0 — Reconfirm implementation baseline (门禁: OBS-G0)

- [x] **OBS-W0-01 — Baseline & Specification Alignment.**
  Workspace canonical specification `docs/standards/logging-and-errors.md` read and accepted as normative authority. Repository topology, develop branches, and dirty worktree safety reconfirmed.
- [x] **OBS-W0-02 — Component Coverage & Machine Protocol Matrix.**
  Identified all running executables, libraries, stdout machine protocols (`cy-package-runtime`, `cy-platform-api`, `cy-manifest`), and authoritative boundaries (Journal vs Audit vs Product Events vs Logs).
- [x] **OBS-W0-03 — Ignored Result Classification.**
  Classified all `let _ =` and unhandled error occurrences in critical Platform paths into the 5 normative categories.
- [x] **OBS-G0 — Wave 0 Baseline Gate.**
  Complete baseline matrix published in repository plan; zero production behavior modification.

---

### Wave 1 — Rust observability foundation (门禁: OBS-G1)

- [x] **OBS-W1-01 — Thin shared helper crate `cy-observability`.**
  Located at `framework/crates/cy-observability`. Encapsulates `tracing` + `tracing-subscriber` configuration, explicit `stderr` writer, JSON and human-readable formatting, level filtering, service metadata (`service.name`, `service.instance.id`), and bounded field helpers. Zero business logic, zero state authority, zero custom RPC.
- [x] **OBS-W1-02 — Formatting, Redaction & Emergency Panic Diagnostics.**
  Deterministic UTC RFC3339 timestamps, secret sanitizer/redactor, emergency panic hook writing minimal unredacted-safe diagnosis to stderr without locks or async queues, and bounded flush.
- [x] **OBS-W1-03 — Platform Executable Integration.**
  Executable binaries (`cyrene-kernel`, `cy-node-agent`, `cy-runtime-agent`, `cyrene-sandboxd`, `cy-package-runtime`) initialize the subscriber on startup. Libraries only emit events/spans.
- [x] **OBS-G1 — Wave 1 Observability Foundation Gate.**
  Subscriber initialization verified; stdout isolation verified for stdio protocols; panic hook verified.

---

### Wave 2 — Error namespace and structured model (门禁: OBS-G2)

- [x] **OBS-W2-01 — Stable Error Code Catalog for Platform.**
  Format: `PLATFORM.<DOMAIN>.<REASON>`. Defined in `cy-observability` with stable machine identifiers (`PLATFORM.KERNEL.*`, `PLATFORM.LEASE.*`, `PLATFORM.WORKER.*`, `PLATFORM.NODE.*`, `PLATFORM.SANDBOX.*`, `PLATFORM.PACKAGE.*`, `PLATFORM.RELAY.*`, `PLATFORM.PANIC.*`).
- [x] **OBS-W2-02 — Strict Structured Event Model.**
  All first-party events conform to schema: `schema_version`, `timestamp`, `level`, `event.name`, `service.name`, `service.instance.id`, `message`, `trace_id`/`span_id`, `attributes`. Normal events do not mandate error codes.
- [x] **OBS-W2-03 — Bounded Record Limits & Sanitization.**
  Enforced 32 KiB record budget, 4 KiB message budget, 8-level cause chain budget. Field whitelist for sensitive contexts.
- [x] **OBS-G2 — Wave 2 Structured Model Gate.**
  JSON schemas validated with positive and negative unit tests; no full env/request/token dumps.

---

### Wave 3 — Critical Platform instrumentation (门禁: OBS-G3)

- [ ] **OBS-W3-A — Kernel Lease, Worker, Persistence & Recovery.**
  Instrument `cyrene-kernel` & `cy-kernel-daemon`: lease acquisition/release, rollback failures, epoch recovery evidence, watchdog reap.
- [ ] **OBS-W3-B — Execution Reconcile & Termination Classification.**
  Instrument reconciliation invocation in `cy-execution-fabric` / `cy-execution-control`: desired vs observed, termination disposition, without INFO spam per loop.
- [ ] **OBS-W3-C — Node Disconnect, Reconnect & Backoff.**
  Instrument `cy-node-agent`: initial failure, rate-limited summary with retry count and elapsed time, recovery event. No ERROR spam per retry.
- [ ] **OBS-W3-D — Sandbox Termination & Cleanup.**
  Instrument `cyrene-sandboxd`: termination signals, grace periods, cgroup removal, unconfirmed cleanup reporting.
- [ ] **OBS-W3-E — Package, Plugin & Adapter Lifecycle.**
  Instrument `cy-package-runtime`: installation, activation, health check, deactivation, rollback failure.
- [ ] **OBS-W3-F — Relay & Control Request Failures.**
  Instrument `cy-workspace-fabric`: peer rejection, frame errors, disconnect/reconnect.
- [ ] **OBS-G3 — Wave 3 Platform Instrumentation Gate.**
  Critical paths observable under normal and failure modes; no log storm in loops.

---

### Wave 4 — Correlation and API mapping (门禁: OBS-G4)

- [ ] **OBS-W4-01 — Correlation Hierarchy.**
  Clean separation of `request_id`, `operation_id`, `resource_id`, and `trace_id`/`span_id`.
- [ ] **OBS-W4-02 — Untrusted Boundary Sanitization & W3C Trace Context.**
  Format validation and bounded lengths for all incoming correlation headers.
- [ ] **OBS-W4-03 — Cross-Product & Plugin Correlation Mapping.**
  Product services (Exchange, Reactor, Catalyst) map their existing logging and API error codes into compatible standard fields without introducing binary dependencies on Platform.
- [ ] **OBS-G4 — Wave 4 Correlation Gate.**
  End-to-end operation tracing demonstrated across boundaries; API error codes mapped.

---

### Wave 5 — Local persistence and bounded failure behavior (门禁: OBS-G5)

- [ ] **OBS-W5-01 — Controlled Local File Sink & Single Rotation Owner.**
  Standard stderr for managed services; dedicated bounded rolling file sink for standalone/unmanaged deployments. Only one rotation owner.
- [ ] **OBS-W5-02 — Bounded Operational Budgets.**
  Fixed queue limits (records & bytes), flush timeout (e.g. 2000ms), file size (e.g. 50 MB), retention count (e.g. 5), per-host budget.
- [ ] **OBS-W5-03 — Failure Mode Hardening.**
  Verify behavior under simulated queue full, disk full, permission denied, slow sink. Logging failures must never deadlock lease release or worker cleanup. Dropped counts tracked.
- [ ] **OBS-G5 — Wave 5 Bounded Resilience Gate.**
  All failure modes tested; bounded memory and non-blocking guarantees proven.

---

### Wave 6 — Acceptance & Governance (门禁: OBS-G6)

- [ ] **OBS-W6-01 — Specification LOG-01 to LOG-14 Compliance.**
  Comprehensive automated test suite covering startup, stdout isolation, lease failures, reconnect rate-limiting, secret redaction, log injection resistance, panic safety, and error code correlation.
- [ ] **OBS-W6-02 — Evidence Record & Joint Acceptance Report.**
  Final evidence compiled with exact command logs, testing scope, and limitations. Ready for RC joint acceptance.
- [ ] **OBS-G6 — Final RC Observability Gate.**
  Joint acceptance readiness achieved.
