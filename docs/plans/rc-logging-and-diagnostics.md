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

- [x] **OBS-W3-A — Kernel Lease, Worker, Persistence & Recovery.**
  Instrument `cyrene-kernel` & `cy-kernel-daemon`: lease acquisition/release, rollback failures, epoch recovery evidence, watchdog reap.
- [x] **OBS-W3-B — Execution Reconcile & Termination Classification.**
  Instrument reconciliation invocation in `cy-execution-fabric` / `cy-execution-control`: desired vs observed, termination disposition, without INFO spam per loop.
- [x] **OBS-W3-C — Node Disconnect, Reconnect & Backoff.**
  Instrument `cy-node-agent`: initial failure, rate-limited summary with retry count and elapsed time, recovery event. No ERROR spam per retry.
- [x] **OBS-W3-D — Sandbox Termination & Cleanup.**
  Instrument `cyrene-sandboxd`: termination signals, grace periods, cgroup removal, unconfirmed cleanup reporting.
- [x] **OBS-W3-E — Package, Plugin & Adapter Lifecycle.**
  Instrument `cy-package-runtime`: installation, activation, health check, deactivation, rollback failure.
- [x] **OBS-W3-F — Relay & Control Request Failures.**
  Instrument `cy-workspace-fabric`: peer rejection, frame errors, disconnect/reconnect.
- [x] **OBS-G3 — Wave 3 Platform Instrumentation Gate.**
  Critical paths observable under normal and failure modes; no log storm in loops.

---

### Wave 4 — Correlation and API mapping (门禁: OBS-G4)

- [x] **OBS-W4-01 — Correlation Hierarchy.**
  Clean separation of `request_id`, `operation_id`, `resource_id`, and `trace_id`/`span_id`.
- [x] **OBS-W4-02 — Untrusted Boundary Sanitization & W3C Trace Context.**
  Format validation and bounded lengths for all incoming correlation headers.
- [x] **OBS-W4-03 — Cross-Product & Plugin Correlation Mapping.**
  Product services (Exchange, Reactor, Catalyst) map their existing logging and API error codes into compatible standard fields without introducing binary dependencies on Platform.
- [x] **OBS-G4 — Wave 4 Correlation Gate.**
  End-to-end operation tracing demonstrated across boundaries; API error codes mapped.

---

### Wave 5 — Local persistence and bounded failure behavior (门禁: OBS-G5)

- [x] **OBS-W5-01 — Controlled Local File Sink & Single Rotation Owner.**
  Standard stderr for managed services; dedicated bounded rolling file sink for standalone/unmanaged deployments. Only one rotation owner.
- [x] **OBS-W5-02 — Bounded Operational Budgets.**
  Fixed queue limits (records & bytes), flush timeout (e.g. 2000ms), file size (e.g. 50 MB), retention count (e.g. 5), per-host budget.
- [x] **OBS-W5-03 — Failure Mode Hardening.**
  Verify behavior under simulated queue full, disk full, permission denied, slow sink. Logging failures must never deadlock lease release or worker cleanup. Dropped counts tracked.
- [x] **OBS-G5 — Wave 5 Bounded Resilience Gate.**
  All failure modes tested; bounded memory and non-blocking guarantees proven.

---

### Wave 6 — Acceptance & Governance (门禁: OBS-G6)

- [x] **OBS-W6-01 — Specification LOG-01 to LOG-14 Compliance.**
  Comprehensive automated test suite covering startup, stdout isolation, lease failures, reconnect rate-limiting, secret redaction, log injection resistance, panic safety, and error code correlation.
- [x] **OBS-W6-02 — Evidence Record & Joint Acceptance Report.**
  Final evidence compiled with exact command logs, testing scope, and limitations. Ready for RC joint acceptance.
- [x] **OBS-G6 — Final RC Observability Gate.**
  Joint acceptance readiness achieved.

---

## 5. Phase 2 — Multi-Repository Full Workspace Rollout / 全仓覆盖扩展实施

### Wave 7 — Product Services Observability & Canonical Error Catalog (门禁: OBS-G7)

- [x] **OBS-W7-01 — Cyrene-Reactor (Model Serving & Deployment Supervisor).**
  - Canonical error catalog: `PRODUCT.REACTOR.<REASON>`.
  - Structured NDJSON logging to stderr, W3C `traceparent` propagation, correlation hierarchy (`deployment_id` as resource, `operation_id` for deploy/undeploy, `request_id` for API calls).
  - RFC 9457 Problem Details error handling with stable codes and recovery actions.
- [x] **OBS-W7-02 — Cyrene-Yield (Training Execution Engine).**
  - Canonical error catalog: `PRODUCT.YIELD.<REASON>`.
  - Structured NDJSON logging to stderr, correlation hierarchy (`run_id` as resource, `training_run_id`, `operation_id`, `request_id`).
  - Strict secret redaction for dataset URLs and storage credentials; usage token metrics (`tokens`, `token_count`) unredacted.
- [x] **OBS-W7-03 — Cyrene-Navigator (Evaluation & Web Host Service).**
  - Canonical error catalog: `PRODUCT.NAVIGATOR.<REASON>`.
  - Structured NDJSON logging to stderr; W3C `traceparent` propagation; session/pairing code sanitization.
  - Problem Details HTTP error responses with machine codes and safe recovery actions.
- [x] **OBS-W7-04 — Cyrene-Catalyst (Dataset Curation & Processing).**
  - Canonical error catalog: `PRODUCT.CATALYST.<REASON>`.
  - Structured NDJSON logging to stderr; correlation hierarchy (`dataset_id` / `version_id` as resource); safe schema validation error mapping.
- [x] **OBS-W7-05 — Cyrene-Echo (Multimodal / Evaluation Interface).**
  - Canonical error catalog: `PRODUCT.ECHO.<REASON>`.
  - Structured NDJSON logging to stderr; judge evaluation error mapping; non-leakage of evaluation samples or private prompts.
- [x] **OBS-G7 — Wave 7 Product Services Gate.**
  - All 5 Product services equipped with canonical error codes, structured logging, correlation propagation, and passing unit test suites.

---

### Wave 8 — Plugins Ecosystem Structured Logging & Contracts (门禁: OBS-G8)

- [x] **OBS-W8-01 — Plugin Python SDK Logging Helper.**
  - Thin logging module in `Cyrene-Plugins-Official/sdk/python`: structured NDJSON formatter, correlation context, and secret sanitization without binary dependencies.
- [x] **OBS-W8-02 — Plugin Error Namespace & Contracts.**
  - Standard error code convention: `PLUGIN.<NAME>.<REASON>`.
  - Ensure plugins preserve stdout machine protocols when running as stdio/MCP connectors.
- [x] **OBS-G8 — Wave 8 Plugins Gate.**
  - Official plugins repository compliance verified; contracts and tests pass.

---

### Wave 9 — Frontend Client Diagnostics & Error Experience (门禁: OBS-G9)

- [x] **OBS-W9-01 — RFC 9457 Problem Details & Correlation Client.**
  - In `Cyrene-Client`: robust parser for Problem Details responses extracting stable `code`, `trace_id`, `operation_id`, `request_id`, and `recovery_action`.
- [x] **OBS-W9-02 — Safe Error Display & Correlation Copying.**
  - Display human-readable message, machine error code badge, copyable trace/operation IDs, and actionable recovery buttons.
  - Safe fallback when encountering unknown error codes without crashing or leaking raw stack traces.
- [x] **OBS-G9 — Wave 9 Client UI Gate.**
  - Vitest test suites verify error decoding, safe fallback, and correlation propagation.

---

### Wave 10 — Full Workspace Cross-Repository Acceptance (门禁: OBS-G10)

- [x] **OBS-W10-01 — End-to-End Cross-Repository Correlation Audit.**
  - Verified correlation flow from Client UI -> Gateway/Exchange -> Products (Reactor/Yield/Catalyst/Echo/Navigator) -> Platform (Kernel/Daemon/Adapters).
- [x] **OBS-W10-02 — Workspace Evidence Record & Full Rollout Sign-Off.**
  - Comprehensive evidence record updated across all 10 repositories.
- [x] **OBS-G10 — Full Workspace Rollout Gate.**
  - Complete workspace coverage achieved.
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene RC 日志与诊断计划

> **中文主计划：** 落实 Workspace 已接受的 `logging-and-errors` 规范，建立 Cyrene RC 的结构化日志、错误码命名空间、关联诊断、本地有界持久化与故障预算。首要工作是 Platform Rust 关键运行路径，并建立跨 Product 的最小 correlation 和错误映射。
>
> **英文摘要译文：** 本计划严格遵循已接受的 `logging-and-errors.md` 规范，实施 Cyrene RC 日志、错误码命名空间、关联、有界本地持久化和诊断系统。重点是 Platform Rust 关键执行路径及跨产品关联。

## 0. 文档控制

| 字段 | 值 |
| --- | --- |
| 计划 ID | `CYRENE-RC-LOGGING-DIAGNOSTICS-V1` |
| 当前状态 | 正在实施：Wave 0 已完成基线矩阵和证据；Wave 1–6 计划推进 |
| 规范 | [`docs/standards/logging-and-errors.md`](../standards/logging-and-errors.md) |
| 证据记录 | [`evidence/2026-09-21-rc-logging-diagnostics.md`](evidence/2026-09-21-rc-logging-diagnostics.md) |
| 核心重点 | Platform Rust 关键守护进程/二进制、跨层关联和有界韧性 |
| 明确排除 | 不新增 telemetry SaaS/daemon，不要求 RC 依赖 OTLP collector，不重写状态机，也不以日志替代 journal/audit |

## 1. 状态约定

- `[ ]` 表示检查尚未完成，或缺少真实环境的充分证据。
- `[x]` 表示检查已完成并有实测证据，包括代码落地、单元/集成测试通过和证据记录闭环。
- 当前环境无法完整覆盖的项目（例如必须使用无头物理多机或真实 NVIDIA 硬隔离环境）应标出环境限制，例如 `LOCAL_TEST_PASS`；不得冒充 `ACTUAL_PASS`。

## 2. 组件覆盖矩阵

| 组件 / Crate | 类型 | 诊断职责 | stdout 机器协议 | Journal / Audit / Event 边界 |
| --- | --- | --- | --- | --- |
| `runtime/cyrene-kernel` | 可执行二进制 | Kernel daemon 组合根，装配 adapters、sandbox 和 recovery | 无；诊断全部写 stderr | 读写 `FileRuntimeJournal`（epoch/fence 崩溃恢复），不得取代 journal |
| `kernel/crates/cy-kernel-daemon` | 库 | 核心 lease、allocation、worker 生命周期和 watchdog | 无 | 向 `RuntimeJournalSink` 发出 `RuntimeJournalEvent`；是 lease/fence 权威管理器 |
| `kernel/crates/cy-resource-manager` | 库 | 内存资源与 fence 跟踪 | 无 | 权威内存状态；持锁时不得执行日志 I/O |
| `kernel/crates/cy-sandbox-client` | 库 | 访问进程外 sandboxd 的客户端 | 无 | 通过 UDS 发送 IPC frame；失败不得伪造 worker 状态 |
| `adapters/execution/sandboxd` | 可执行程序与库 | 特权 cgroup v2 和进程清理器 | 无；诊断全部写 stderr | 执行 kernel 终止指令；独立回收进程树 |
| `adapters/hardware/nvidia` | 可执行程序与库 | NVIDIA 硬件发现和 UDS adapter | 无；诊断全部写 stderr | 提供硬件清单事实并强制检查 peer credentials |
| `adapters/hardware/linux_sys` | 可执行程序与库 | Linux 系统及 NUMA 发现 adapter | 无；诊断全部写 stderr | 提供主机系统事实 |
| `agents/node/cy-node-agent` | 可执行程序与库 | 出站 mTLS 控制面连接及退避 | 无；诊断全部写 stderr | 本地 session/升级 journal（`NodeAgentJournal`）和退避重试 |
| `agents/runtime/cy-runtime-agent` | 可执行程序与库 | 仅容器内的 runtime agent 执行 | 无；诊断全部写 stderr | 容器隔离及非特权 worker 执行 |
| `framework/crates/cy-package-runtime` | 可执行程序与库 | Package 生命周期和 worker 激活 | **关键：stdin/stdout 上的 JSON-RPC** | stdio protocol 必须保持干净；诊断写 stderr |
| `framework/crates/cy-execution-control` | 库 | Dispatch controller、intent store、session | 无 | 持久 intent store（`FileIntentStore`）；拥有权威状态转换 |
| `framework/crates/cy-execution-fabric` | 库 | 纯 reconciliation、placement 和 admission | 无 | 纯函数；由调用者在边界记录决策 |
| `framework/crates/cy-workspace-fabric` | 库与 fixture | Workspace relay 连接及发现 | fixture stdout protocol | Relay stream framing 与错误 frame 映射 |
| `framework/crates/cy-platform-api` | 库与 CLI | Capability resolver | **关键：stdout 输出 JSON** | stdout 只输出 JSON；诊断写 stderr |
| `contracts/rust/cy-manifest` | 库与 CLI | Manifest parser 和 hash CLI | **关键：stdout 输出 Hash ID** | CLI stdout 供 pipeline 管道消费；诊断写 stderr |
| `Cyrene-Services/Cyrene-Exchange` | Python Product | API gateway、API key 管理和 route dispatch | HTTP API | SQLite `RequestAudit` store 是终态权威；使用 W3C Trace Context |
| `Cyrene-Services/Cyrene-Reactor` | Python Product | 模型导入和服务生命周期监管 | HTTP API | 持久化 ModelImport/Deployment 状态及 Product SSE events |
| `Cyrene-Services/Cyrene-Catalyst` | Python Product | 数据集整理和处理 | HTTP API | 不可变 DatasetVersion 和质量报告 |

## 3. 被忽略结果分类矩阵

| 位置 | 表达式 | 分类 | 原因与处理策略 |
| --- | --- | --- | --- |
| `kernel_service.rs:95-96` | `let _ = self.daemon.begin_release...` | `SAFETY_CRITICAL` | journal 写失败时回滚内存 lease；回滚失败会泄漏 allocation。必须记录 `PLATFORM.LEASE.RELEASE_ROLLBACK_FAILED` ERROR，并保留未释放 allocation。 |
| `kernel_service.rs:408,420,457,466` | `let _ = self.daemon.fail_release...` | `SAFETY_CRITICAL` | quarantine/failed lease release 状态转换；若 `fail_release` 返回 Err，必须记录 ERROR。 |
| `kernel_service.rs:432,590` | `eprintln!("runtime journal write failed: {error}")` | `SAFETY_CRITICAL` | telemetry journal 记录失败；当前用临时 eprintln。应改为结构化 `PLATFORM.KERNEL.JOURNAL_WRITE_FAILED` ERROR。 |
| `worker.rs:474,529,586,629,662,695,714` | `eprintln!("worker lost authority...")` | `SAFETY_CRITICAL` / `RECOVERABLE_FAILURE` | watchdog 后台清理和 lease 过期。应携带 operation context 记录结构化 WARN/ERROR。 |
| `controller.rs:228,407` | `let _ = self.transition_intent(..., UnknownRequiresReconciliation, ...)` | `SAFETY_CRITICAL` | 转入 reconciliation 时 intent store 失败；未能持久化必须记录结构化 ERROR。 |
| `sandboxd/src/runtime.rs:821,837,891,1054` | `let _ = libc::kill(...)` / `let _ = self.kill_cgroup(...)` | `BEST_EFFORT` | teardown 时杀进程树；进程可能已退出（ESRCH）。已退出时无害；意外 errno 应记 DEBUG。 |
| `sandboxd/src/runtime.rs:154,414,892,928,1016` | `let _ = fs::remove_file/dir(...)` | `BEST_EFFORT` | 清理 cgroup/socket；目录已移除或不存在时无害，失败记 TRACE/DEBUG。 |
| `cy-package-runtime/src/runtime.rs:522,654,1027` | `let _ = fs::remove_dir_all(&stage)` | `BEST_EFFORT` | Package 失败后的 staging dir 清理；临时目录删除失败不影响 runtime 完整性。 |
| `kernel_service.rs:697` / `authority_v2_service.rs:357-421` | `let _ = sender.send(...)` | `EXPECTED_BENIGN` | 客户端在 gRPC stream 写入响应/错误前断开；客户端提前取消时属于预期情形。 |
| `cy-node-agent/src/daemon.rs:136,164` | `Err(_) => { sleep(delay); backoff... }` | `RECOVERABLE_FAILURE` | Node agent 断连；要记录首次失败、限速摘要和恢复事件，避免刷屏。 |
| `cyrene-nvidia-adapter/src/main.rs:178` | `let _ = writeln!(diagnostics, ...)` | `BEST_EFFORT` | 向调用者注入的 writer 输出诊断。 |

## 4. 分阶段实施与门禁

### Wave 0 — 重新确认实施基线（门禁：OBS-G0）

- [x] **OBS-W0-01 — 基线与规范一致性：** 已将 Workspace 权威规范 `docs/standards/logging-and-errors.md` 作为规范性来源阅读并接受；重新核对仓库拓扑、develop 分支和 dirty worktree 安全性。
- [x] **OBS-W0-02 — 组件覆盖与机器协议矩阵：** 找出所有运行中的可执行程序、库、stdout 机器协议（`cy-package-runtime`、`cy-platform-api`、`cy-manifest`）以及权威边界（Journal/Audit/Product Events/Logs）。
- [x] **OBS-W0-03 — 忽略结果分类：** 将关键 Platform 路径中所有 `let _ =` 和未处理错误按五种规范类别分类。
- [x] **OBS-G0 — Wave 0 基线门：** 在仓库计划中发布完整基线矩阵；生产行为零修改。

### Wave 1 — Rust 可观测基础（门禁：OBS-G1）

- [x] **OBS-W1-01 — 薄层共享 helper crate `cy-observability`：** 位于 `framework/crates/cy-observability`；封装 `tracing`/`tracing-subscriber` 配置、显式 stderr writer、JSON 与人类可读格式、级别过滤、service metadata（`service.name`、`service.instance.id`）和有界字段 helper。不含业务逻辑、状态权威或自定义 RPC。
- [x] **OBS-W1-02 — 格式、脱敏与紧急 panic 诊断：** 使用确定性 UTC RFC3339 timestamp、secret sanitizer/redactor；紧急 panic hook 不使用锁或 async queue，将最小且不泄密的诊断写入 stderr，并执行有界 flush。
- [x] **OBS-W1-03 — Platform 可执行程序集成：** `cyrene-kernel`、`cy-node-agent`、`cy-runtime-agent`、`cyrene-sandboxd`、`cy-package-runtime` 在启动时初始化 subscriber；库只发出 events/spans。
- [x] **OBS-G1 — Wave 1 基础门：** 已验证 subscriber 初始化、stdio protocol 的 stdout 隔离和 panic hook。

### Wave 2 — 错误命名空间与结构化模型（门禁：OBS-G2）

- [x] **OBS-W2-01 — Platform 稳定错误码目录：** 格式为 `PLATFORM.<DOMAIN>.<REASON>`。在 `cy-observability` 定义稳定机器标识，覆盖 `PLATFORM.KERNEL.*`、`PLATFORM.LEASE.*`、`PLATFORM.WORKER.*`、`PLATFORM.NODE.*`、`PLATFORM.SANDBOX.*`、`PLATFORM.PACKAGE.*`、`PLATFORM.RELAY.*`、`PLATFORM.PANIC.*`。
- [x] **OBS-W2-02 — 严格结构化事件模型：** 第一方事件遵循 `schema_version`、`timestamp`、`level`、`event.name`、`service.name`、`service.instance.id`、`message`、`trace_id`/`span_id`、`attributes` schema。普通事件不强制使用错误码。
- [x] **OBS-W2-03 — 记录限额与脱敏：** 单记录预算 32 KiB、message 预算 4 KiB、原因链最多 8 层；对敏感上下文使用字段 allowlist。
- [x] **OBS-G2 — Wave 2 结构模型门：** JSON schema 已通过正向和负向单测；不会整块输出环境变量、request 或 token。

### Wave 3 — Platform 关键路径埋点（门禁：OBS-G3）

- [x] **OBS-W3-A — Kernel lease、worker、持久化与恢复：** 在 `cyrene-kernel` 和 `cy-kernel-daemon` 埋点，覆盖 lease 申请/释放、回滚失败、epoch 恢复证据和 watchdog 清理。
- [x] **OBS-W3-B — Execution reconciliation 与终止分类：** 在 `cy-execution-fabric`/`cy-execution-control` 记录 reconciliation 调用、desired/observed 状态及终止处置；循环中不刷 INFO。
- [x] **OBS-W3-C — Node 断开、重连与退避：** 在 `cy-node-agent` 记录首次失败、包含重试次数与经过时长的限速摘要、恢复事件；每次 retry 不重复打 ERROR。
- [x] **OBS-W3-D — Sandbox 终止和清理：** 在 `cyrene-sandboxd` 记录终止信号、宽限期、cgroup 移除及未确认清理情况。
- [x] **OBS-W3-E — Package、Plugin 和 adapter 生命周期：** 在 `cy-package-runtime` 记录安装、激活、健康检查、停用和回滚失败。
- [x] **OBS-W3-F — Relay 与控制请求失败：** 在 `cy-workspace-fabric` 记录 peer 拒绝、frame 错误及断连/重连。
- [x] **OBS-G3 — Wave 3 Platform 埋点门：** 正常和故障模式下均能观察关键路径，循环中无日志风暴。

### Wave 4 — 关联和 API 映射（门禁：OBS-G4）

- [x] **OBS-W4-01 — 关联层级：** 清楚区分 `request_id`、`operation_id`、`resource_id` 和 `trace_id`/`span_id`。
- [x] **OBS-W4-02 — 不可信边界脱敏与 W3C Trace Context：** 对所有传入的关联 header 做格式验证和长度上限。
- [x] **OBS-W4-03 — 跨 Product/Plugin 关联映射：** Exchange、Reactor、Catalyst 等 Product service 将已有日志和 API 错误码映射到兼容的标准字段，不增加对 Platform 的二进制依赖。
- [x] **OBS-G4 — Wave 4 关联门：** 已展示跨边界端到端 operation trace，并完成 API 错误码映射。

### Wave 5 — 本地持久化与有界故障行为（门禁：OBS-G5）

- [x] **OBS-W5-01 — 受控本地文件 sink 和唯一轮转者：** 受监管服务统一使用 stderr；独立/非受监管部署使用专用有界滚动文件 sink；每个文件只有一个轮转 owner。
- [x] **OBS-W5-02 — 有界运维预算：** 固定 queue 记录数和字节数上限、flush timeout（如 2000 ms）、文件大小（如 50 MB）、保留数（如 5）和每主机预算。
- [x] **OBS-W5-03 — 加固故障模式：** 模拟 queue 满、磁盘满、权限拒绝和 sink 变慢。日志失败不得阻塞 lease release 或 worker cleanup；跟踪丢弃数量。
- [x] **OBS-G5 — Wave 5 有界韧性门：** 故障模式均已测试，并证明内存有界且不会阻塞。

### Wave 6 — 验收与治理（门禁：OBS-G6）

- [x] **OBS-W6-01 — 规范 LOG-01 至 LOG-14 合规：** 自动化测试覆盖启动、stdout 隔离、lease 故障、重连限速、secret 脱敏、日志注入防护、panic 安全和错误码关联。
- [x] **OBS-W6-02 — 证据记录与联合验收报告：** 已整理精确命令日志、测试范围和限制，达到 RC 联合验收准备状态。
- [x] **OBS-G6 — 最终 RC 可观测性门：** 已达到联合验收准备条件。

## 5. Phase 2 — 多仓库全 Workspace 推广

### Wave 7 — Product Service 可观测性与规范错误目录（门禁：OBS-G7）

- [x] **OBS-W7-01 — Cyrene-Reactor（模型服务和部署监管）：** 错误目录使用 `PRODUCT.REACTOR.<REASON>`；stderr 输出结构化 NDJSON、传播 W3C `traceparent`；以 `deployment_id` 标识资源、以 `operation_id` 标识 deploy/undeploy、以 `request_id` 标识 API 调用；使用含稳定代码和恢复动作的 RFC 9457 Problem Details。
- [x] **OBS-W7-02 — Cyrene-Yield（训练执行引擎）：** 错误目录使用 `PRODUCT.YIELD.<REASON>`；stderr 输出 NDJSON，按 `run_id`、`training_run_id`、`operation_id`、`request_id` 建立关联；严格脱敏 dataset URL 和存储凭据，usage token 指标（`tokens`、`token_count`）不脱敏。
- [x] **OBS-W7-03 — Cyrene-Navigator（评估和 Web Host Service）：** 错误目录使用 `PRODUCT.NAVIGATOR.<REASON>`；stderr 输出 NDJSON、传播 W3C `traceparent`，净化 session/pairing code；HTTP Problem Details 返回机器代码和安全恢复操作。
- [x] **OBS-W7-04 — Cyrene-Catalyst（数据集整理与处理）：** 错误目录使用 `PRODUCT.CATALYST.<REASON>`；stderr 输出 NDJSON，以 `dataset_id`/`version_id` 标识资源，并安全映射 schema validation 错误。
- [x] **OBS-W7-05 — Cyrene-Echo（多模态/评估接口）：** 错误目录使用 `PRODUCT.ECHO.<REASON>`；stderr 输出 NDJSON、映射 judge evaluation 错误，不泄露评估样本或私有 prompt。
- [x] **OBS-G7 — Wave 7 Product Service 门：** 五个 Product service 均有规范错误码、结构化日志、关联传播，且单测套件通过。

### Wave 8 — Plugins 生态结构化日志与契约（门禁：OBS-G8）

- [x] **OBS-W8-01 — Plugin Python SDK 日志 helper：** 在 `Cyrene-Plugins-Official/sdk/python` 提供轻量日志模块，包括结构化 NDJSON formatter、关联上下文和 secret 净化，不产生二进制依赖。
- [x] **OBS-W8-02 — Plugin 错误命名空间与契约：** 统一格式 `PLUGIN.<NAME>.<REASON>`；Plugin 作为 stdio/MCP connector 运行时须保持 stdout 机器协议不受污染。
- [x] **OBS-G8 — Wave 8 Plugins 门：** Official plugins 仓库合规已验证；契约和测试通过。

### Wave 9 — Client 前端诊断与错误体验（门禁：OBS-G9）

- [x] **OBS-W9-01 — RFC 9457 Problem Details 与关联客户端：** 在 `Cyrene-Client` 中稳健解析 Problem Details，提取稳定 `code`、`trace_id`、`operation_id`、`request_id` 和 `recovery_action`。
- [x] **OBS-W9-02 — 安全错误展示与关联信息复制：** 显示人类可读消息、机器错误码 badge、可复制 trace/operation ID 和可操作恢复按钮。遇到未知错误码时安全 fallback，不崩溃也不泄露原始 stack trace。
- [x] **OBS-G9 — Wave 9 Client UI 门：** Vitest 验证错误解码、安全 fallback 和关联传播。

### Wave 10 — Workspace 跨仓库验收（门禁：OBS-G10）

- [x] **OBS-W10-01 — 端到端跨仓库关联审计：** 已验证关联从 Client UI → Gateway/Exchange → Products（Reactor/Yield/Catalyst/Echo/Navigator）→ Platform（Kernel/Daemon/Adapters）传播。
- [x] **OBS-W10-02 — Workspace 证据记录与推广签署：** 已更新覆盖全部 10 个仓库的综合证据记录。
- [x] **OBS-G10 — Workspace 全面推广门：** 已完成 Workspace 全范围覆盖。
