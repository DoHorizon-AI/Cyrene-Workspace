# Aether Migration: WSL Acceptance Runbook

This runbook defines the local acceptance boundary for the Aether feature migration. It keeps services, data, toolchains, and evidence on the current Ubuntu 24.04 WSL2 x86_64 host. The cloud VM, cloud services, production ingress, and production Bot configuration are outside the test target.

## Acceptance host and private state

The task-scoped Compose project is `cyrene-aether-wsl-acceptance`. Its private state is `/home/baijin/.local/state/cyrene-aether-wsl-acceptance/`, created with owner-only permissions. Node 24.13.0 and pnpm 11.7.0 are installed under that directory after checking the official Node SHA-256 and npm package integrity; the user's global versions are not changed.

| Service | Local address | Access rule |
|---|---|---|
| Navigator | `127.0.0.1:18680` | Local browser and test client |
| Navigator Persistence | `127.0.0.1:18681` | Local services only |
| Exchange | `127.0.0.1:18682` | Local model gateway |
| Client | `127.0.0.1:18684` | Browser acceptance |
| Control | `127.0.0.1:18685` | Local service control |
| Local Qwen chat model | `aether-qwen:8080` on the task network | CPU inference; WSL process URL is recorded in private state |
| Local E5 embedding model | `aether-embedding:8081` on the task network | CPU inference; WSL process URL is recorded in private state |
| PostgreSQL + pgvector | `postgres:5432` on the task's internal Docker network | No host port is published; attach service containers to the task network |

The PostgreSQL password is generated locally and kept only in `postgres.env` with mode `0600`. Do not put it in command arguments, test output, screenshots, browser traces, or Git. Database files and sanitized evidence stay under the task state directory. Existing Docker containers, networks, volumes, and BuildKit state must not be reused or removed.

## Setup and operation

Run these commands from the Workspace worktree:

```bash
python3 scripts/aether_wsl_acceptance.py doctor
python3 scripts/aether_wsl_acceptance.py toolchain
python3 scripts/aether_wsl_acceptance.py browser-deps --playwright-package <installed-playwright-package-directory>
python3 scripts/aether_wsl_acceptance.py up
python3 scripts/aether_wsl_acceptance.py models prepare
python3 scripts/aether_wsl_acceptance.py models up
python3 scripts/aether_wsl_acceptance.py status
```

The `up` command is restricted to Ubuntu 24.04 WSL2 x86_64 and a local Unix-socket Docker context. It checks that the fixed task subnet `10.247.253.0/24` does not overlap a current Docker network or WSL route, then starts only the project-labelled PostgreSQL dependency. The Compose file pins the pgvector image by manifest digest, uses an internal project network, publishes no port, and bind-mounts only the task's new state directory. To stop this service, use `python3 scripts/aether_wsl_acceptance.py down --confirm-project cyrene-aether-wsl-acceptance`; this leaves database files intact.

`browser-deps` verifies Ubuntu package-index hashes, downloads the missing Chromium libraries without installing packages, and extracts them under the task state directory. When given an installed Playwright package directory, it also launches Chromium, reads a local data-page title, and closes the browser. Source the generated environment file for Playwright commands:

```bash
source /home/baijin/.local/state/cyrene-aether-wsl-acceptance/toolchain/browser-libs.env
```

To use the task-local frontends in a shell, set `PATH` for that shell only:

```bash
TASK_TOOLS=/home/baijin/.local/state/cyrene-aether-wsl-acceptance/toolchain
export PATH="$TASK_TOOLS/node-v24.13.0-linux-x64/bin:$TASK_TOOLS/pnpm-11.7.0/bin:$PATH"
node --version
pnpm --version
```

The expected database endpoint from a container attached to the internal network is host `postgres`, port `5432`, database/user `cyrene_aether`. Read the password from the private `postgres.env` file through the consuming service's secret-file mechanism. Do not copy it into a tracked `.env` file. Host-published PostgreSQL ports are intentionally absent.

`models prepare` downloads an official Qwen2.5 0.5B Q4_K_M GGUF, a multilingual E5 small quantized ONNX model, and its tokenizer to the private state directory. Each file is pinned to an immutable Hugging Face revision and SHA-256. `models up` builds the pinned Python CPU embedding service, starts it with the pinned `llama.cpp` server, and verifies a real local chat generation plus two 384-dimensional normalized embeddings. Model services publish no host ports. Containers use `http://aether-qwen:8080/v1` and `http://aether-embedding:8081/v1`; the host-run Exchange process can source `/home/baijin/.local/state/cyrene-aether-wsl-acceptance/evidence/models-host.env` to use the current private bridge addresses. E5 inputs should include `query: ` or `passage: ` prefixes as appropriate; the local endpoint embeds the supplied text unchanged. The Qwen model is Apache-2.0; the E5 model is MIT ([Qwen model](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF), [E5 model](https://huggingface.co/intfloat/multilingual-e5-small)).

Before starting product services, attach their acceptance containers to the existing network created by Compose and publish only the planned loopback ports. Confirm that ports `18680`, `18681`, `18682`, `18684`, and `18685` are free before starting those services. The `doctor` command reports port availability without contacting cloud Cyrene endpoints.

## P0–P10 behavior coverage matrix

Each row ties a test stimulus to an observable result, the code area that must own the behavior, and a named evidence record. Once implementation candidates are fixed, record the exact repository SHA and replace the expected code area with exact source paths. A fixture, mock, or fake host is marked `SIMULATED`; it cannot satisfy a `REAL` gate.

| Stage / gate | Input or action | Required observable result | Implementation location to trace | Acceptance evidence |
|---|---|---|---|---|
| P0 / isolation | Run `doctor`, create the isolated DB, inspect Compose labels, network and ports | Ubuntu 24.04 WSL2 x64 is confirmed; only task-labelled resources are created; DB has no host port; old Docker state remains untouched | Workspace `scripts/aether_wsl_acceptance.py`, `tooling/aether-wsl-acceptance/compose.yaml`; deployment workflow guard owned by the responsible repository | `P0/isolation`, Docker inspection summary and `doctor` output |
| P0 / boundary | Query the running local stack's endpoint/config inventory | All acceptance calls target loopback or local task-network endpoints; no cloud VM, ACA, production ingress, or production Bot target is selected | Workspace profile/config; Navigator local host configuration | `P0/endpoint-boundary`, sanitized endpoint inventory |
| P0 / local model runtime | Call the local Qwen chat and E5 embeddings APIs with fixed smoke prompts | Real Qwen response and two finite, normalized 384-dimensional multilingual vectors return over the isolated network; no model host port is published | Workspace model harness; official Qwen GGUF and E5 ONNX artifacts | `P0/local-model-endpoints`, model revisions, file hashes, and inference receipt |
| P1 / memory persistence | Save a memory through the formal plugin client, restart the plugin, then read it | The same scoped record and provenance are read after process restart | Plugins memory provider contract/client and SQLite or pgvector provider | `P1/memory-restart`, request/result IDs and DB reconciliation |
| P1 / tenant isolation | Store same-text records under separate account/session/persona scopes and query each scope | Each query returns only authorized scope records | Plugins memory provider; Navigator scope binding | `P1/memory-isolation`, per-scope result counts |
| P1 / vector integrity | Embed distinct Chinese and English queries/documents with the pinned local embedding model | Real nonzero vectors with fixed model revision/dimension are persisted; nearest-neighbor ordering matches the test corpus | Exchange embeddings API and Plugins memory provider | `P1/embedding-retrieval`, model manifest, dimensions and ranked IDs |
| P1 / delete/export/import | Delete one record, export a scoped set, and re-import the export twice | Deleted record is absent; export reconciles counts; second import is idempotent | Plugins memory provider and package CLI | `P1/memory-lifecycle`, before/after counts and import report |
| P2 / L1 and summary | Send enough messages to cross the configured short-context boundary | A real local or authorized model produces a summary that preserves named facts, source message IDs and session scope | Navigator conversation-memory pipeline; Exchange model route | `P2/l1-summary`, input/output digests and source references |
| P2 / long-term recall | Ask a later question whose answer exists only in a prior message | Reply includes the correct fact and a retrievable source reference after restart | Navigator extraction/context assembly; Plugins memory provider | `P2/l2-recall`, message chain and cited memory ID |
| P2 / revoke and delete | Revoke the source grant, query again, then delete the source memory | Revoked/deleted content is excluded from retrieval and prompt context | Navigator authorization checks and memory lifecycle API | `P2/revoke-delete`, authorization read-back and zero-result evidence |
| P2 / Web flow | Use the real local Client/browser to create, inspect, correct and delete a memory | UI changes agree with Navigator API and durable store after reload | Client memory views/API client; Navigator Work API | `P2/web-browser`, browser trace with secrets redacted and server read-back |
| P2 / Teams flow | Send a message through an approved dedicated development callback and read its reply | Real inbound event, memory use, outbound result and callback receipt reconcile | Teams development ingress; Navigator conversation pipeline; Client diagnostics | `P2/teams-real`; `DEFERRED_BY_USER / NOT_RUN` until the user configures a dedicated test callback |
| P3 / persona binding | Bind two test sessions to different personas and ask the same memory-backed question | Each response applies only its persona configuration and associated authorized profile | Navigator persona resolver and profile APIs; Client controls | `P3/persona-scope`, session/persona IDs and response read-back |
| P3 / profile lifecycle | Create a profile candidate from sourced evidence, reject/apply it, then roll back | Candidate remains auditable; only approved version affects responses; rollback restores the prior version | Navigator profile lifecycle; Client review UI | `P3/profile-review-rollback`, version and evidence references |
| P3 / temporary state | Add a time-bounded state and query before and after expiry | It is usable before expiry and excluded after expiry without erasing its audit history | Navigator profile expiry/maintenance task | `P3/profile-expiry`, timestamps and retrieval results |
| P4 / graph provenance | Extract entities/relations from two sources that contain similar text | Graph query returns distinct source identities, relation evidence and permitted neighbors | Plugins graph provider contract; Navigator extraction/context policy | `P4/graph-provenance`, node/edge IDs and source references |
| P4 / knowledge package | Publish/select a versioned Catalyst package, then search it through a chat query | Search result points to package version and source; unauthorized package content is excluded | Catalyst package lineage; Plugins retrieval; Navigator chat consumer | `P4/knowledge-citation`, package digest and result references |
| P4 / duplicate merge | Submit equivalent entities from separate sources and run merge preview/apply | Merge preserves all source links and reversible audit; duplicate execution is idempotent | Navigator graph maintenance and Plugins graph provider | `P4/graph-merge`, preview/apply report and source count reconciliation |
| P5 / dream bounded work | Run maintenance over a multi-page fixture and inspect memory/resource bounds | Processing is paginated, checkpointed, cancellable and resumes without duplicate mutations | Navigator DSH maintenance task; Plugins paginated storage | `P5/dream-recovery`, page/checkpoint counts and restart report |
| P5 / dream change control | Preview and apply a merge, contradiction, expiry and deletion batch | Every change has source/version rationale, current-authorization check, audit and supported restore | Navigator dream planner/apply path; Client review screen | `P5/dream-audit-restore`, plan digest, audit entries and restore read-back |
| P6 / research sources | Search and fetch a paginated public document, then cite selected content | Returned material is real, bounded, source-addressed and separated from private chat history | Plugins public-web/document tools; Navigator DSH research tasks | `P6/research-provenance`, source URL, page cursor and content digest |
| P6 / background cancellation | Start a slow read task, cancel it, then deliver a late provider result | Cancelled result is retained as cancelled and cannot be injected or published | Navigator task runner and tool adapter | `P6/cancel-late-result`, task state and absence of context mutation |
| P6 / work notes | Save a work note and a proposed operating guide separately | Notes retain origin and trust state; proposed guidance does not become trusted fact automatically | Navigator work-document/task state; Client review | `P6/work-note-trust`, record types and read-back |
| P7 / Nous handoff | Create a promise, wait condition, active task, then hand off/restart the session | Durable handoff restores unfinished work and unknown external outcomes without replay | Navigator DSH session/task/schedule; Client task state | `P7/nous-handoff`, task IDs and before/after state |
| P7 / proactive policy | Trigger follow-up, then disable subscription or enter quiet hours before send | No message is sent after policy change; current grants are checked at send time | Navigator proactive behavior and send gate; connector receipt path | `P7/proactive-send-gate`, policy read-back and connector result |
| P7 / group hygiene | Feed burst, deleted, self-authored and loop-shaped events | Debounce/merge/retraction/loop rules produce the documented action and do not create duplicate replies | Navigator input/output policy; message connector | `P7/group-hygiene`, event-to-action ledger |
| P8 / speech | Submit an authorized Chinese/English test clip to the local CPU SenseVoice runtime | Real transcript, language/status and source attachment reference are returned; failure is explicit | Plugins speech provider; local SenseVoice runtime; Navigator media adapter | `P8/asr-real`, model manifest, audio digest and transcript result |
| P8 / image understanding | Attach a real test image to a message and request a grounded description | Model result identifies the attachment source; unsupported media fails clearly | Exchange multimodal route; Navigator attachment context | `P8/image-real`, attachment digest and response reference |
| P8 / media directory | Add, search, deactivate and restore an authorized sticker/image | Search respects account/persona scope; generation/storage is separate from sending | Plugins media provider/tool; Client media UI | `P8/media-lifecycle`, object IDs and status read-back |
| P8 / generated card | Render a card and inspect its artifact, then separately send through a local test connector | Artifact digest/version is stable; send receipt is a separate result | Plugins media tool; Navigator artifact/send APIs | `P8/card-and-send`, artifact digest and independent send receipt |
| P9 / QQ | Use the self-owned QQ Connector and an approved test Host/account for login and message round trip | Host/account readiness, private/group message and read-back reconcile | Plugins QQ connector/runtime; Navigator connector binding; Client status | `P9/qq-real`; `DEFERRED_BY_USER / NOT_RUN` until the user configures a dedicated account |
| P9 / Bilibili | Search/read, then perform an approved test comment/message and read it back | Every external write has a durable intent/result and independent platform read-back | Plugins Bilibili connector; Exchange account authorization; Navigator task/receipt | `P9/bilibili-real`; `DEFERRED_BY_USER / NOT_RUN` until the user configures a dedicated account |
| P10 / backup restore | Back up local task data, restore to a fresh isolated location, and reconcile all counts | Restored memory, profile, graph and task state matches the manifest without importing secrets | Workspace operations recipe; Navigator/Plugins backup adapters | `P10/backup-restore`, manifest digest and record/task counts |
| P10 / offline import | Dry-run a selected export, import it twice, revoke a source and roll back the batch | Mapping/counts are explicit; unauthorized records are quarantined; retry is idempotent; batch rollback is bounded | Navigator import command; Plugins provider export/import; Client diagnostics | `P10/offline-import`, dry-run and reconciliation reports |
| P10 / daily review | Run a daily review over a bounded window and inspect generated report | Review names evidence gaps and accepted sources; it does not feed private conversation logs into Catalyst automatically | Navigator review task; Catalyst only for user-selected sources; Client report UI | `P10/daily-review`, input window and report/source manifest |

## Evidence rules and current state

Store acceptance artifacts in `/home/baijin/.local/state/cyrene-aether-wsl-acceptance/evidence/`. Prefer small JSON receipts, API read-backs, sanitized browser results, source hashes and count reconciliations. Do not store raw authorization headers, credentials, private chat dumps, or unredacted browser storage. The `record` command stores only the evidence path, SHA-256, candidate commit IDs and classification; it never reads or prints evidence contents.

The initial WSL readiness gates are `PASS / REAL`: isolated PostgreSQL, the task-local Node/pnpm toolchain, Playwright Chromium launch, and local Qwen/E5 inference. These establish test infrastructure only; P1–P10 product behavior remains unaccepted until fixed implementation candidates pass their gates. Teams, QQ and Bilibili external traffic is `DEFERRED_BY_USER / NOT_RUN` while dedicated accounts are configured later.

The first model-connector integration receipt records real local Qwen/E5 inference and HTTP 200 through the candidate model-api-connector and Exchange gateway. Route planning, test-principal resolution, and audit observation in that run were simulated, so the persisted Product route, production identity, durable audit, and fixed `18682` service remain `NOT_RUN / ACCEPTANCE_PENDING`. The receipt and the separate `REAL` / `SIMULATED` classifications are in `model-api-connector-exchange-5afc0f4.json` and `evidence.jsonl`.

Use these classifications consistently:

| Status | Mode | Meaning |
|---|---|---|
| `PASS` or `FAIL` | `REAL` | Executed through the candidate product/plugin against the isolated local service or approved real endpoint |
| `PASS` or `FAIL` | `SIMULATED` | Executed with a fixture, mock provider, or fake host; it does not prove a real external integration |
| `NOT_RUN` | `NOT_RUN` | Not executed, blocked, or waiting for implementation; add a reason code |
| `NOT_RUN` + `DEFERRED_BY_USER` | `NOT_RUN` | Dedicated Teams, QQ, or Bilibili credentials/accounts are intentionally configured later by the user |

Example after placing a sanitized receipt under the private evidence directory:

```bash
python3 scripts/aether_wsl_acceptance.py record \
  --stage P1 --gate embedding-retrieval --status PASS --mode REAL \
  --candidate Plugins=<40-to-64-hex-sha> \
  --candidate Exchange=<40-to-64-hex-sha> \
  --evidence /home/baijin/.local/state/cyrene-aether-wsl-acceptance/evidence/p1-embedding.json
```

External platform gates use `--status NOT_RUN --mode NOT_RUN --reason DEFERRED_BY_USER`. Do not interpret a simulated or package-level result as product acceptance. Full product gates start only after the main thread supplies the fixed candidate SHAs for the relevant repositories.

---
<!-- Chinese Translation / 中文翻译 -->

# Aether 迁移：WSL 验收操作手册

本手册定义 Aether 功能迁移的本地验收边界。服务、数据、工具链和验收证据均保存在当前 Ubuntu 24.04 WSL2 x86_64 主机。云端 VM、云服务、生产入口和生产 Bot 配置不属于本次测试目标。

## 验收主机与私有状态

任务专属 Compose 项目名为 `cyrene-aether-wsl-acceptance`。私有状态目录为 `/home/baijin/.local/state/cyrene-aether-wsl-acceptance/`，权限仅限当前用户。Node 24.13.0 和 pnpm 11.7.0 在核对 Node 官方 SHA-256 及 npm 包完整性后安装到此目录，不修改用户全局版本。

| 服务 | 本地地址 | 访问规则 |
|---|---|---|
| Navigator | `127.0.0.1:18680` | 本地浏览器和测试客户端 |
| Navigator Persistence | `127.0.0.1:18681` | 仅供本地服务访问 |
| Exchange | `127.0.0.1:18682` | 本地模型网关 |
| Client | `127.0.0.1:18684` | 浏览器验收 |
| Control | `127.0.0.1:18685` | 本地服务控制 |
| 本地 Qwen 聊天模型 | 任务网络中的 `aether-qwen:8080` | CPU 推理；WSL 地址记录在私有状态目录 |
| 本地 E5 向量模型 | 任务网络中的 `aether-embedding:8081` | CPU 推理；WSL 地址记录在私有状态目录 |
| PostgreSQL + pgvector | 任务内部 Docker 网络的 `postgres:5432` | 不发布主机端口；服务容器须加入该网络 |

PostgreSQL 密码在本机生成，仅保存于权限为 `0600` 的 `postgres.env`。不要把它放入命令参数、测试输出、截图、浏览器 trace 或 Git。数据库文件和脱敏证据保存在任务状态目录。不得复用或删除已有 Docker 容器、网络、卷和 BuildKit 状态。

## 准备与运行

在 Workspace worktree 中运行：

```bash
python3 scripts/aether_wsl_acceptance.py doctor
python3 scripts/aether_wsl_acceptance.py toolchain
python3 scripts/aether_wsl_acceptance.py browser-deps --playwright-package <已安装的 Playwright 包目录>
python3 scripts/aether_wsl_acceptance.py up
python3 scripts/aether_wsl_acceptance.py models prepare
python3 scripts/aether_wsl_acceptance.py models up
python3 scripts/aether_wsl_acceptance.py status
```

`up` 仅适用于 Ubuntu 24.04 WSL2 x86_64 和本地 Unix socket Docker context。启动前会检查固定任务子网 `10.247.253.0/24` 是否与现有 Docker network 或 WSL 路由重叠，然后只启动带任务标签的 PostgreSQL 依赖。Compose 文件按镜像清单 digest 锁定 pgvector，使用项目内部网络，不发布端口，并且只绑定新的任务状态目录。停止该服务时运行 `python3 scripts/aether_wsl_acceptance.py down --confirm-project cyrene-aether-wsl-acceptance`；数据库文件会保留。

`browser-deps` 会校验 Ubuntu 包索引中的哈希，下载缺失的 Chromium 库但不安装系统包，并解包到任务状态目录。传入已安装 Playwright 包目录后，它还会启动 Chromium、读取本地 data 页面标题并关闭浏览器。`models prepare` 将固定版本的 Qwen2.5 0.5B Q4_K_M GGUF、多语种 E5 small 量化 ONNX 模型及 tokenizer 下载到私有状态目录，并逐个校验 SHA-256。`models up` 构建固定 Python CPU embedding 服务，启动 `llama.cpp` server，并通过真实推理验证本地聊天响应和两个 384 维归一化向量。模型服务不发布主机端口。容器使用 `http://aether-qwen:8080/v1` 与 `http://aether-embedding:8081/v1`；在 WSL 主机运行的 Exchange 可加载 `/home/baijin/.local/state/cyrene-aether-wsl-acceptance/evidence/models-host.env`，使用当前私有 bridge 地址。E5 输入应按用途包含 `query: ` 或 `passage: ` 前缀；endpoint 不会改写输入。Qwen 模型许可为 Apache-2.0，E5 为 MIT（[Qwen 模型](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF)、[E5 模型](https://huggingface.co/intfloat/multilingual-e5-small)）。运行 Playwright 命令前加载生成的环境文件：

```bash
source /home/baijin/.local/state/cyrene-aether-wsl-acceptance/toolchain/browser-libs.env
```

在当前 shell 临时使用任务专用工具链：

```bash
TASK_TOOLS=/home/baijin/.local/state/cyrene-aether-wsl-acceptance/toolchain
export PATH="$TASK_TOOLS/node-v24.13.0-linux-x64/bin:$TASK_TOOLS/pnpm-11.7.0/bin:$PATH"
node --version
pnpm --version
```

加入内部网络的容器通过 `postgres:5432` 访问数据库，数据库名和用户均为 `cyrene_aether`。应用通过自己的 secret-file 机制读取私有 `postgres.env` 中的密码；不要复制到跟踪的 `.env` 文件。PostgreSQL 不提供主机端口映射。

启动产品服务之前，把相应服务容器接入 Compose 创建的内部网络，并且只发布计划中的 loopback 端口。启动产品服务前确认 `18680`、`18681`、`18682`、`18684` 和 `18685` 未被占用。`doctor` 只检查端口可用性，不会访问云端 Cyrene endpoint。

## P0–P10 行为覆盖矩阵

每行将测试输入、可观察结果、行为归属代码区域和证据编号关联。候选提交固定后，记录对应仓库的精确 SHA，并把预期代码区域替换为实际源文件路径。fixture、mock 和 fake host 标记为 `SIMULATED`，不能满足 `REAL` 验收。

| 阶段 / 验收项 | 输入或操作 | 必须观察到的结果 | 追踪实现位置 | 验收证据 |
|---|---|---|---|---|
| P0 / 隔离 | 运行 `doctor`，启动隔离数据库，检查 Compose 标签、网络和端口 | 确认 Ubuntu 24.04 WSL2 x64；只创建带任务标签的资源；数据库无主机端口；旧 Docker 状态未改变 | Workspace `scripts/aether_wsl_acceptance.py`、`tooling/aether-wsl-acceptance/compose.yaml`；部署工作流保护由对应仓库负责人实现 | `P0/isolation`、Docker 检查摘要和 `doctor` 输出 |
| P0 / 边界 | 查询本地运行栈的 endpoint/config 清单 | 验收调用只指向 loopback 或本地任务网络；不选择云 VM、ACA、生产入口或生产 Bot | Workspace profile/config；Navigator 本地 host 配置 | `P0/endpoint-boundary`、脱敏 endpoint 清单 |
| P0 / 本地模型服务 | 使用固定 smoke 输入调用本地 Qwen chat 和 E5 embeddings API | 返回真实 Qwen 回复及两个有限、归一化的 384 维向量；模型服务不发布主机端口 | Workspace 模型 harness；官方 Qwen GGUF 与 E5 ONNX 制品 | `P0/local-model-endpoints`、模型 revision/文件哈希和推理回执 |
| P1 / 记忆持久化 | 通过正式插件客户端保存记忆，重启插件后读取 | 重启后读取到相同范围的记录和来源信息 | Plugins memory provider 契约/客户端及 SQLite 或 pgvector 实现 | `P1/memory-restart`、请求/结果 ID 和数据库对账 |
| P1 / 数据隔离 | 在不同账号/会话/persona 范围写入相同文本并分别查询 | 各查询只返回其获授权范围内的记录 | Plugins memory provider；Navigator scope 绑定 | `P1/memory-isolation`、各范围结果计数 |
| P1 / 向量完整性 | 用固定本地 embedding 模型为中英查询和文档生成向量 | 保存真实非零向量，绑定模型版本/维度；测试语料最近邻排序正确 | Exchange embedding API；Plugins memory provider | `P1/embedding-retrieval`、模型清单、维度和排序 ID |
| P1 / 删除/导出/导入 | 删除一条记录、导出指定范围，再导入两次 | 删除后不可读；导出数量对账；第二次导入幂等 | Plugins memory provider 和包 CLI | `P1/memory-lifecycle`、前后计数和导入报告 |
| P2 / L1 摘要 | 发送足够消息以越过短上下文边界 | 真实本地或授权模型生成摘要，并保留关键事实、来源消息 ID 和会话范围 | Navigator conversation-memory pipeline；Exchange model route | `P2/l1-summary`、输入/输出摘要和来源引用 |
| P2 / 长期召回 | 提问一个答案只在早先消息中出现的问题 | 重启后仍能召回正确事实和可读取的来源引用 | Navigator 提取/上下文组装；Plugins memory provider | `P2/l2-recall`、消息链和记忆 ID |
| P2 / 撤销与删除 | 撤销来源授权后再次查询，再删除来源记忆 | 被撤销/删除内容不再进入检索和提示词 | Navigator 授权检查和记忆生命周期 API | `P2/revoke-delete`、授权回读和零结果证据 |
| P2 / Web 流程 | 用真实本地 Client/浏览器创建、查看、更正和删除记忆 | UI 状态与 Navigator API、持久化存储一致，刷新后仍一致 | Client 记忆页面/API 客户端；Navigator Work API | `P2/web-browser`、脱敏浏览器 trace 和服务端回读 |
| P2 / Teams 流程 | 通过获批准的开发回调发消息并读取回复 | 真实入站事件、记忆使用、出站结果和回调回执对账 | Teams 开发 ingress；Navigator 对话 pipeline；Client 诊断 | `P2/teams-real`；用户配置专用测试回调前为 `DEFERRED_BY_USER / NOT_RUN` |
| P3 / Persona 绑定 | 两个测试会话绑定不同 persona，询问同一个记忆问题 | 每个回复只应用自己的 persona 配置和获授权画像 | Navigator persona resolver/profile API；Client 控制界面 | `P3/persona-scope`、会话/persona ID 和回复回读 |
| P3 / 画像生命周期 | 从带来源证据创建候选画像，执行拒绝/应用/回滚 | 候选过程可审计；只有已批准版本生效；回滚恢复旧版本 | Navigator 画像生命周期；Client 审核界面 | `P3/profile-review-rollback`、版本及证据引用 |
| P3 / 临时状态 | 添加有时限的状态，在过期前后查询 | 过期前可用、过期后排除，审计记录保留 | Navigator 画像过期/维护任务 | `P3/profile-expiry`、时间戳和检索结果 |
| P4 / 图谱来源 | 从两份相似文本中提取实体/关系并查询 | 图谱结果保留不同来源身份、关系证据及有权访问的邻居 | Plugins graph provider contract；Navigator 提取/上下文策略 | `P4/graph-provenance`、节点/边 ID 和来源引用 |
| P4 / 知识包 | 发布/选择 Catalyst 知识包版本，再从聊天检索 | 结果带包版本和来源；未授权内容被过滤 | Catalyst package lineage；Plugins retrieval；Navigator chat consumer | `P4/knowledge-citation`、包摘要和结果引用 |
| P4 / 重复项合并 | 提交不同来源的等价实体并预览/应用合并 | 保留全部来源关系；操作可审计、可恢复且重复执行幂等 | Navigator 图谱维护；Plugins graph provider | `P4/graph-merge`、预览/应用报告和来源计数 |
| P5 / Dream 有界处理 | 对多页 fixture 执行维护并检查内存/进度 | 分页、检查点、可取消；重启恢复且不重复变更 | Navigator DSH maintenance task；Plugins 分页存储 | `P5/dream-recovery`、页/检查点计数和重启报告 |
| P5 / Dream 变更控制 | 预览并应用合并、冲突、过期和删除批次 | 每项变化有来源/版本说明、当前授权检查、审计和恢复方式 | Navigator dream planner/apply；Plugins 存储 | `P5/dream-audit-restore`、计划摘要、审计项和恢复回读 |
| P6 / 研究来源 | 搜索、分页读取公开文档并引用选中内容 | 返回真实、受限、有来源标识的材料，并与私聊日志隔离 | Plugins public-web/document tools；Navigator DSH research tasks | `P6/research-provenance`、来源 URL、分页游标和内容摘要 |
| P6 / 后台取消 | 启动慢速读取后取消，再让 provider 返回迟到结果 | 任务保持取消状态；迟到结果不能注入上下文或发布 | Navigator task runner 和 tool adapter | `P6/cancel-late-result`、任务状态和未发生上下文变更的证明 |
| P6 / 工作笔记 | 分别保存工作笔记与待审核操作指南 | 笔记保留来源和信任状态；待审指南不会自动成为可信事实 | Navigator work-document/task state；Client 审核 | `P6/work-note-trust`、记录类型和读取回执 |
| P7 / Nous 交接 | 创建承诺、等待条件和活跃任务后交接/重启会话 | 持久交接恢复未完成工作和未知外部结果，不自动重放 | Navigator DSH session/task/schedule；Client 任务状态 | `P7/nous-handoff`、任务 ID 和前后状态 |
| P7 / 主动行为策略 | 触发跟进，再在发送前关闭订阅或进入安静时段 | 策略变更后不发送；发送时检查当前授权 | Navigator proactive behavior/send gate；connector receipt | `P7/proactive-send-gate`、策略回读和 connector 结果 |
| P7 / 群消息卫生 | 输入突发、撤回、自发及循环形状事件 | 防抖、合并、撤回和循环规则产生预期动作，不重复回复 | Navigator 输入/输出策略；消息 connector | `P7/group-hygiene`、事件到动作账本 |
| P8 / 语音 | 将获授权的中英测试音频提交到本地 CPU SenseVoice runtime | 返回真实转写、语言/状态和附件来源；错误明确报告 | Plugins speech provider；本地 SenseVoice runtime；Navigator media adapter | `P8/asr-real`、模型清单、音频摘要和转写结果 |
| P8 / 图片理解 | 把真实测试图片附加到消息并请求有依据的描述 | 模型结果能指向附件来源；不支持的媒体明确失败 | Exchange multimodal route；Navigator attachment context | `P8/image-real`、附件摘要和回复引用 |
| P8 / 媒体目录 | 添加、搜索、停用、恢复一张获授权表情/图片 | 搜索遵守账号/persona 范围；生成/保存与发送分离 | Plugins media provider/tool；Client media UI | `P8/media-lifecycle`、对象 ID 和状态回读 |
| P8 / 卡片生成 | 生成卡片并检查制品，再通过本地测试 connector 单独发送 | 制品摘要/版本稳定；发送回执是独立结果 | Plugins media tool；Navigator artifact/send APIs | `P8/card-and-send`、制品摘要和独立发送回执 |
| P9 / QQ | 用自有 QQ Connector 和获批测试 Host/账号登录并往返消息 | Host/账号状态、私聊/群聊消息和平台回读对账 | Plugins QQ connector/runtime；Navigator connector binding；Client 状态 | `P9/qq-real`；用户配置专用账号前为 `DEFERRED_BY_USER / NOT_RUN` |
| P9 / B站 | 搜索/读取后，通过获批测试账号执行评论/私信并回读 | 外部写入有持久意图/结果和独立平台回读 | Plugins Bilibili connector；Exchange 账号授权；Navigator task/receipt | `P9/bilibili-real`；用户配置专用账号前为 `DEFERRED_BY_USER / NOT_RUN` |
| P10 / 备份恢复 | 备份本地任务数据，在新的隔离位置恢复并对账 | 恢复后的记忆、画像、图谱和任务状态与清单一致，不导入密钥 | Workspace 运维手册；Navigator/Plugins 备份 adapter | `P10/backup-restore`、清单摘要和记录/任务计数 |
| P10 / 离线导入 | 对选定导出试运行、重复导入、撤销来源并回滚批次 | 映射/计数明确；未授权数据隔离；重试幂等；批次回滚有界 | Navigator 导入命令；Plugins provider 导入/导出；Client 诊断 | `P10/offline-import`、试运行及对账报告 |
| P10 / 每日复盘 | 对有界时间窗执行复盘并查看报告 | 报告标明证据缺口和可信来源；不会自动把私聊日志送入 Catalyst | Navigator review task；Catalyst 仅处理用户选定来源；Client 报告页面 | `P10/daily-review`、输入时间窗和报告来源清单 |

## 证据规则与当前状态

把验收产物放在 `/home/baijin/.local/state/cyrene-aether-wsl-acceptance/evidence/`。优先保存小型 JSON 回执、API 回读、脱敏浏览器结果、源码摘要和计数对账。不要保存原始授权头、凭据、私聊全文或未脱敏浏览器存储。`record` 命令只保存证据路径、SHA-256、候选提交和分类；不会读取或打印证据内容。

当前 WSL 基础设施门禁为 `PASS / REAL`：隔离 PostgreSQL、任务专用 Node/pnpm、Playwright Chromium 启动以及本地 Qwen/E5 推理。这些仅证明验收环境可用；P1–P10 产品行为仍须等固定实现候选通过对应门禁后才能验收。用户稍后配置专用账号前，Teams、QQ 和 Bilibili 外部流量记为 `DEFERRED_BY_USER / NOT_RUN`。

首轮模型 connector 集成回执记录了候选 model-api-connector 与 Exchange gateway 对本地 Qwen/E5 的真实调用及 HTTP 200。该轮的 route planner、测试 principal 和 audit observer 使用模拟实现，因此持久化 Product route、生产身份、持久审计和固定 `18682` 服务仍为 `NOT_RUN / ACCEPTANCE_PENDING`。回执及拆分后的 `REAL` / `SIMULATED` 分类记录在 `model-api-connector-exchange-5afc0f4.json` 与 `evidence.jsonl`。

按以下分类记录：

| 状态 | 模式 | 含义 |
|---|---|---|
| `PASS` 或 `FAIL` | `REAL` | 通过候选产品/插件执行，并使用隔离本地服务或获批真实 endpoint |
| `PASS` 或 `FAIL` | `SIMULATED` | 使用 fixture、mock provider 或 fake host；不证明真实外部集成 |
| `NOT_RUN` | `NOT_RUN` | 未运行、受阻或等待实现；同时填写原因码 |
| `NOT_RUN` + `DEFERRED_BY_USER` | `NOT_RUN` | Teams、QQ 或 Bilibili 专用账号/凭据由用户稍后配置 |

在私有证据目录放置脱敏回执后，可以这样记录：

```bash
python3 scripts/aether_wsl_acceptance.py record \
  --stage P1 --gate embedding-retrieval --status PASS --mode REAL \
  --candidate Plugins=<40-to-64-hex-sha> \
  --candidate Exchange=<40-to-64-hex-sha> \
  --evidence /home/baijin/.local/state/cyrene-aether-wsl-acceptance/evidence/p1-embedding.json
```

外部平台验收使用 `--status NOT_RUN --mode NOT_RUN --reason DEFERRED_BY_USER`。不得把模拟结果或包级测试解释为产品验收。完整产品验收须等主线程提供相关仓库的固定候选 SHA 后开始。
