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
python3 scripts/aether_wsl_acceptance.py identity prepare
python3 scripts/aether_wsl_acceptance.py speech prepare
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

`identity prepare` creates independent random Product data-plane, Product control, Product endpoint-bootstrap, Navigator session, Work attachment, and local Control credentials under `local-product/`, plus an owner-only strict Navigator principal map. The identities are scoped to actor `aether-local-acceptance`, organization `cyrene-aether-local`, and workspace `local`; the attachment principal is actor-private and read-only. It also writes `embedding-profile.json` with the E5 model revision and digest, 384-dimensional vector binding, and `passage: `/`query: ` prefixes. The command prints paths and scope names only, never token values. This enables a real local static-Bearer acceptance path and does not configure production Directory or OIDC identity.

`speech prepare` installs the hash-pinned `sherpa-onnx` CPU wheel and its runtime packages only under the task state, unpacks the pinned `imageio-ffmpeg` wheel there, and downloads the sherpa-compatible SenseVoice int8 ONNX model, tokenizer, and official `zh.wav` fixture at immutable revision `2365baeacb507f821a0c8120fcee3d484dba7a07`. It records wheel, model, tokenizer, audio, FFmpeg binary hashes, and runtime versions in `models/speech/manifest.json`. The model is governed by the [FunASR Model Open Source License Agreement](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE), including source/model attribution and keeping the model name. The bundled FFmpeg binary reports GPLv3; both are used only in local acceptance state and are not packaged into Cyrene releases.

Run a real provider-process check against exact candidate checkouts with:

```bash
python3 scripts/aether_wsl_acceptance.py speech smoke \
  --plugin-source <plugins-tools>/plugins/providers/speech-sensevoice \
  --contracts-sdk <plugins-contracts>/sdk/python \
  --expected-plugin-sha <full-40-character-sha> \
  --expected-contracts-sha <full-40-character-sha>
```

The command starts the formal provider bootstrap bound to an ephemeral `127.0.0.1` DirectPluginRuntime endpoint, sends the fixture through the typed gRPC client, checks the typed response, and stops the child process. It does not call a Product route or a cloud endpoint. The repaired provider candidate normalizes the language to `zh` and passes local CPU inference, but the returned sample has a one-character recognition difference from the upstream expected sentence; provider transport/inference passes while recognition quality remains a separate review item. The current receipt records Plugins `96568cff65ddefed9ffe086127ebd6aa0d2ad3df` and Contracts `79a3984dd362c58b5d20fa8ce5ea972408148748`.

Before starting product services, attach their acceptance containers to the existing network created by Compose and publish only the planned loopback ports. Confirm that ports `18680`, `18681`, `18682`, `18684`, and `18685` are free before starting those services. The `doctor` command reports port availability without contacting cloud Cyrene endpoints.

## P0–P10 behavior coverage matrix

Each row ties a test stimulus to an observable result, the code area that must own the behavior, and a named evidence record. Once implementation candidates are fixed, record the exact repository SHA and replace the expected code area with exact source paths. Mark a result `SIMULATED` when a fixture, mock, or fake host substitutes for the capability or host being tested. A fixture used only as input to a real model/plugin/product execution remains `REAL`; record the input provenance separately. A component-level `REAL` result does not satisfy a broader product-flow gate.

| Stage / gate | Input or action | Required observable result | Implementation location to trace | Acceptance evidence |
|---|---|---|---|---|
| P0 / isolation | Run `doctor`, create the isolated DB, inspect Compose labels, network and ports | Ubuntu 24.04 WSL2 x64 is confirmed; only task-labelled resources are created; DB has no host port; old Docker state remains untouched | Workspace `scripts/aether_wsl_acceptance.py`, `tooling/aether-wsl-acceptance/compose.yaml`; deployment workflow guard owned by the responsible repository | `P0/isolation`, Docker inspection summary and `doctor` output |
| P0 / boundary | Query the running local stack's endpoint/config inventory | All acceptance calls target loopback or local task-network endpoints; no cloud VM, ACA, production ingress, or production Bot target is selected | Workspace profile/config; Navigator local host configuration | `P0/endpoint-boundary`, sanitized endpoint inventory |
| P0 / local model runtime | Call the local Qwen chat and E5 embeddings APIs with fixed smoke prompts | Real Qwen response and two finite, normalized 384-dimensional multilingual vectors return over the isolated network; no model host port is published | Workspace model harness; official Qwen GGUF and E5 ONNX artifacts | `P0/local-model-endpoints`, model revisions, file hashes, and inference receipt |
| P1 / memory persistence | Save a memory through the formal plugin client, restart the plugin, then read it | The same scoped record and provenance are read after process restart | Plugins memory provider contract/client and SQLite or pgvector provider | `P1/memory-restart`, request/result IDs and DB reconciliation |
| P1 / tenant isolation | Store same-text records under separate account/session/persona scopes and query each scope | Each query returns only authorized scope records | Plugins memory provider; Navigator scope binding | `P1/memory-isolation`, per-scope result counts |
| P1 / vector integrity | Embed distinct Chinese and English queries/documents with the pinned local embedding model | Real nonzero vectors with fixed model revision/dimension are persisted; nearest-neighbor ordering matches the test corpus | Exchange embeddings API and Plugins memory provider | `P1/embedding-retrieval`, model manifest, dimensions and ranked IDs |
| P1 / Product E5 embedding route | Send passage/query inputs through the authenticated Product Gateway after service restart | Persisted scoped route is visible; the real E5 response preserves model identity and input indices and returns finite, nonzero vectors at the profile dimension | Exchange Product Gateway route planner; model-api-connector and E5 service | `P1/exchange-product-embedding-route`; does not by itself prove memory persistence or recall |
| P1 / E5 memory-provider component | Use the local E5 adapter and plugin runtime to exercise all seven memory RPCs, then reopen SQLite and read back | Real 384D E5 vectors are finite/nonzero; store/get/recall/delete/prune/export/import work and records survive SQLite reopen | Navigator `DirectEmbeddingClient`; Plugins model connector and memory SQLite provider | `P1/memory-e5-directplugin-sqlite-component`; temporary host composition and Product/Work authorization remain separate gates |
| P1 / pgvector provider component | Run the memory-pgvector provider suite against the isolated PostgreSQL service with deterministic test vectors | All seven RPCs cross the typed runtime boundary; scope, migration, pagination, cancellation and pool recovery tests pass | Plugins memory-pgvector provider and task-isolated PostgreSQL | `P1/memory-pgvector-integration-suite`; test vectors are fixtures and official PackageSpec installation is not established |
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
| P4 / graph-provider component | Upsert nodes/edges, read each back independently, query source references, reopen SQLite and read again | Graph method results and source-linked records survive SQLite reopen with matching digests | Navigator `DirectKnowledgeGraphProvider`; Plugins knowledge-graph SQLite provider | `P4/graph-directplugin-sqlite-component`; authenticated Work ACL remains a separate gate |
| P4 / knowledge package | Publish/select a versioned Catalyst package, then search it through a chat query | Search result points to package version and source; unauthorized package content is excluded | Catalyst package lineage; Plugins retrieval; Navigator chat consumer | `P4/knowledge-citation`, package digest and result references |
| P4 / duplicate merge | Submit equivalent entities from separate sources and run merge preview/apply | Merge preserves all source links and reversible audit; duplicate execution is idempotent | Navigator graph maintenance and Plugins graph provider | `P4/graph-merge`, preview/apply report and source count reconciliation |
| P5 / dream bounded work | Run maintenance over a multi-page fixture and inspect memory/resource bounds | Processing is paginated, checkpointed, cancellable and resumes without duplicate mutations | Navigator DSH maintenance task; Plugins paginated storage | `P5/dream-recovery`, page/checkpoint counts and restart report |
| P5 / dream change control | Preview and apply a merge, contradiction, expiry and deletion batch | Every change has source/version rationale, current-authorization check, audit and supported restore | Navigator dream planner/apply path; Client review screen | `P5/dream-audit-restore`, plan digest, audit entries and restore read-back |
| P6 / research sources | Search and fetch a paginated public document, then cite selected content | Returned material is real, bounded, source-addressed and separated from private chat history | Plugins public-web/document tools; Navigator DSH research tasks | `P6/research-provenance`, source URL, page cursor and content digest |
| P6 / search backend component | Query the restricted local search backend on two pages and inspect result sources | Real JSON results carry source URLs and page boundaries from the configured engine allowlist | Task-local SearXNG service and isolated search adapter | `P6/searxng-live-json-pagination`; backend component only, not a Navigator/DSH task |
| P6 / background cancellation | Start a slow read task, cancel it, then deliver a late provider result | Cancelled result is retained as cancelled and cannot be injected or published | Navigator task runner and tool adapter | `P6/cancel-late-result`, task state and absence of context mutation |
| P6 / work notes | Save a work note and a proposed operating guide separately | Notes retain origin and trust state; proposed guidance does not become trusted fact automatically | Navigator work-document/task state; Client review | `P6/work-note-trust`, record types and read-back |
| P7 / Nous handoff | Create a promise, wait condition, active task, then hand off/restart the session | Durable handoff restores unfinished work and unknown external outcomes without replay | Navigator DSH session/task/schedule; Client task state | `P7/nous-handoff`, task IDs and before/after state |
| P7 / proactive policy | Trigger follow-up, then disable subscription or enter quiet hours before send | No message is sent after policy change; current grants are checked at send time | Navigator proactive behavior and send gate; connector receipt path | `P7/proactive-send-gate`, policy read-back and connector result |
| P7 / group hygiene | Feed burst, deleted, self-authored and loop-shaped events | Debounce/merge/retraction/loop rules produce the documented action and do not create duplicate replies | Navigator input/output policy; message connector | `P7/group-hygiene`, event-to-action ledger |
| P8 / speech | Send the immutable upstream `zh.wav` fixture through the local provider process and typed DirectPluginRuntime client | A real nonempty transcript, language/status, duration and segments return; typed failures remain explicit. The current fixture result differs by one character from the documented sample text, so quality review remains open | Plugins `speech-sensevoice` provider; task-local sherpa-onnx CPU runtime; Navigator media adapter remains a separate gate | `P8/asr-real`, exact Plugins/Contracts SHAs, model/token/audio and FFmpeg hashes, typed response receipt |
| P8 / vision model component | Send two generated PNG inputs directly through the real local vision model connector and DirectPlugin | Both inputs produce model responses; record that the inputs are synthetic fixtures | Exchange model connector; task-local llama.cpp and pinned SmolVLM weights | `P8/vision-model-inference`, model/connector hashes and input provenance; does not pass attachment or authorization flow |
| P8 / image understanding | Attach a real test image to a message and request a grounded description | Model result identifies the attachment source; unsupported media fails clearly | Exchange multimodal route; Navigator attachment context | `P8/image-real`, attachment digest and response reference |
| P8 / media directory | Add, search, deactivate and restore an authorized sticker/image | Search respects account/persona scope; generation/storage is separate from sending | Plugins media provider/tool; Client media UI | `P8/media-lifecycle`, object IDs and status read-back |
| P8 / generated card | Render a card and inspect its artifact, then separately send through a local test connector | Artifact digest/version is stable; send receipt is a separate result | Plugins media tool; Navigator artifact/send APIs | `P8/card-and-send`, artifact digest and independent send receipt |
| P9 / QQ | Use the self-owned QQ Connector and an approved test Host/account for login and message round trip | Host/account readiness, private/group message and read-back reconcile | Plugins QQ connector/runtime; Navigator connector binding; Client status | `P9/qq-real`; `DEFERRED_BY_USER / NOT_RUN` until the user configures a dedicated account |
| P9 / Bilibili | Search/read, then perform an approved test comment/message and read it back | Every external write has a durable intent/result and independent platform read-back | Plugins Bilibili connector; Exchange account authorization; Navigator task/receipt | `P9/bilibili-real`; `DEFERRED_BY_USER / NOT_RUN` until the user configures a dedicated account |
| P10 / backup restore | Back up local task data, restore to a fresh isolated location, and reconcile all counts | Restored memory, profile, graph and task state matches the manifest without importing secrets | Workspace operations recipe; Navigator/Plugins backup adapters | `P10/backup-restore`, manifest digest and record/task counts |
| P10 / offline import | Dry-run a selected export, import it twice, revoke a source and roll back the batch | Mapping/counts are explicit; unauthorized records are quarantined; retry is idempotent; batch rollback is bounded | Navigator import command; Plugins provider export/import; Client diagnostics | `P10/offline-import`, dry-run and reconciliation reports |
| P10 / daily review | Run a daily review over a bounded window and inspect generated report | Review names evidence gaps and accepted sources; it does not feed private conversation logs into Catalyst automatically | Navigator review task; Catalyst only for user-selected sources; Client report UI | `P10/daily-review`, input window and report/source manifest |

## Evidence rules and current state

Store acceptance artifacts in `/home/baijin/.local/state/cyrene-aether-wsl-acceptance/evidence/`. Prefer small JSON receipts, API read-backs, sanitized browser results, source hashes and count reconciliations. Do not store raw authorization headers, credentials, private chat dumps, or unredacted browser storage. The `record` command stores only the evidence path, SHA-256, candidate commit IDs and classification; it never prints evidence contents. A `PASS` or `FAIL` record must reference at least one owned, regular, non-symlink evidence file with a recognized suffix and bounded size; gate names are restricted to lowercase slugs. `NOT_RUN` records may contain only the required reason and no evidence.

The initial WSL readiness gates are `PASS / REAL`: isolated PostgreSQL, the task-local Node/pnpm toolchain, Playwright Chromium launch, and local Qwen/E5 inference. The SenseVoice provider subgate also passes as a real candidate plugin process invocation, while its sample transcript has a one-character difference from the upstream expected text. These component checks do not establish the Navigator attachment-to-context flow or full P8 product acceptance; P1–P10 product behavior remains unaccepted until fixed implementation candidates pass their gates. Teams, QQ and Bilibili external traffic is `DEFERRED_BY_USER / NOT_RUN` while dedicated accounts are configured later.

The first model-connector integration receipt records real local Qwen/E5 inference and HTTP 200 through the candidate model-api-connector and Exchange gateway. Route planning, test-principal resolution, and audit observation in that run were simulated, so the persisted Product route, production identity, durable audit, and fixed `18682` service remain `NOT_RUN / ACCEPTANCE_PENDING`. The receipt and the separate `REAL` / `SIMULATED` classifications are in `model-api-connector-exchange-5afc0f4.json` and `evidence.jsonl`.

### Live WSL evidence update (receipts through 2026-11-01 UTC)

This snapshot supersedes the earlier pending note only for the named gates below. The fixed local Exchange process first used scoped-route candidate `5d5c68d30b353a15d486e639cbb3a7e7745f0d51`, model connector source `b700c61deff4382e8e47ad67d22abc91b28da798`, and SDK candidate `8e271a0942ade7d5d7a197c79e71e500da7f6bf7`. A Product-created endpoint and two scoped routes were persisted through the route-draft/confirm API. The authenticated model list and Qwen chat call returned HTTP 200 (`PASS / REAL`, `exchange-product-chat-5d5c68d.json`). That Exchange candidate returned HTTP 404 for `/v1/embeddings` (`FAIL / REAL`, `exchange-product-embedding-5d5c68d-fail.json`). After switching only the local Exchange process to candidate `48c272304e6334e243317c044f4a228a8316103a` and preserving its SQLite state, `/readyz` and persisted model-list read-back returned HTTP 200; the authenticated Product embedding route then returned model `intfloat/multilingual-e5-small`, ordered indices `[0,1]`, and two finite, nonzero 384D vectors with unit norms (`PASS / REAL`, `exchange-product-e5-embeddings-48c2723.json`). The earlier 404 is a retained regression receipt for the superseded candidate; the current Exchange Product embedding route gate passes, while full memory retrieval remains a separate check.

Separately, Navigator's `DirectEmbeddingClient` called the real local E5 model through the model connector, and the memory SQLite provider completed all seven RPCs with SQLite reopen/read-back (`PASS / REAL`, `navigator-adapter-real-direct-plugin-sqlite.json`). The graph SQLite provider also passed upsert, independent node/edge reads, source-reference query, and reopen/read-back (`PASS / REAL`, same receipt). Exact adapter SHA is `b8229c45af4efe0a400ea68d8b79112388868846`, storage provider candidate is `01776eadcd8309f39b2853697ad9ea166533adda`, model connector is `b700c61deff4382e8e47ad67d22abc91b28da798`, and Plugins SDK is `f96b28c3e10233526f78b1355b302732a7aa1b31`. This used a temporary loopback host composition and temporary SQLite databases; the receipt marks host composition simulated and removes temporary data after the test. It does not prove Navigator persistence startup wiring, authenticated Work source/group ACL, browser product flow, or official PackageSpec installation. Product E5 embeddings are now verified by the separate Exchange receipt above; P1 end-to-end memory recall through that route remains pending.

The isolated SearXNG backend returned real JSON search results across two pages from its deliberately restricted Wikipedia-only engine allowlist (`PASS / REAL`, `searxng-live-json-pagination.json`). The PublicWeb DirectPlugin also completed live search/open against that backend and parsed a local TXT fixture in a child process (`PASS / SIMULATED`, `public-web-searxng-real-and-parser-0ae782b.json`): its caller was a local harness rather than the Navigator/DSH product path, and the document was a fixture. These component receipts do not pass the P6 product task, authorization, or attachment bridge gates.

The pinned SmolVLM 256M CPU model produced descriptions for generated image inputs through the real model connector and DirectPlugin (`PASS / REAL`, `smolvlm-256m-real-model-inference-fixture-input.json`; the detailed receipt is `smolvlm-256m-real-image-inference.json`). The inputs are synthetic fixtures, while model execution is real. This proves local image inference only; it does not prove a user attachment reaches Navigator or Exchange, or that product authorization is enforced. The task Compose health probe initially targeted the wrong port; after changing it to the server's `8082/health` endpoint, the container became healthy (`smolvlm-compose-health.json`). The SenseVoice bootstrap/typed gRPC inference remains `PASS / REAL` at the provider component boundary; the official sample transcript differs by one character from the documented expectation (`speech-sensevoice-real.json`).

The PostgreSQL provider test suite completed 17 tests against the isolated database (`database-test-20261101T003639Z-be7b31da.log`, Plugins source candidate `47784d422442b7353250b9e59bebf4a9d8d40a71`). The run used a source-composed provider runtime and host-supplied deterministic vectors; it does not establish official PackageSpec installation or E5-backed Product memory retrieval. The Product E5 embedding endpoint now passes independently, but memory persistence and recall through the persisted Exchange route still need a full Product check. Navigator/Client Web turn and browser acceptance had not run at this snapshot. Teams, QQ, and Bilibili remain `DEFERRED_BY_USER / NOT_RUN`; cloud VM, ACA, production ingress, and production Bot configuration remain outside the test boundary and unchanged.

Use these classifications consistently:

| Status | Mode | Meaning |
|---|---|---|
| `PASS` or `FAIL` | `REAL` | Executed through the candidate product/plugin against the isolated local service or approved real endpoint |
| `PASS` or `FAIL` | `SIMULATED` | A fixture, mock provider, or fake host substitutes for the capability/host being tested; it does not prove that behavior. A fixture used only as input to a real execution is recorded separately and may still be `REAL` |
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
python3 scripts/aether_wsl_acceptance.py identity prepare
python3 scripts/aether_wsl_acceptance.py speech prepare
python3 scripts/aether_wsl_acceptance.py status
```

`up` 仅适用于 Ubuntu 24.04 WSL2 x86_64 和本地 Unix socket Docker context。启动前会检查固定任务子网 `10.247.253.0/24` 是否与现有 Docker network 或 WSL 路由重叠，然后只启动带任务标签的 PostgreSQL 依赖。Compose 文件按镜像清单 digest 锁定 pgvector，使用项目内部网络，不发布端口，并且只绑定新的任务状态目录。停止该服务时运行 `python3 scripts/aether_wsl_acceptance.py down --confirm-project cyrene-aether-wsl-acceptance`；数据库文件会保留。

`browser-deps` 会校验 Ubuntu 包索引中的哈希，下载缺失的 Chromium 库但不安装系统包，并解包到任务状态目录。传入已安装 Playwright 包目录后，它还会启动 Chromium、读取本地 data 页面标题并关闭浏览器。`models prepare` 将固定版本的 Qwen2.5 0.5B Q4_K_M GGUF、多语种 E5 small 量化 ONNX 模型及 tokenizer 下载到私有状态目录，并逐个校验 SHA-256。`models up` 构建固定 Python CPU embedding 服务，启动 `llama.cpp` server，并通过真实推理验证本地聊天响应和两个 384 维归一化向量。模型服务不发布主机端口。容器使用 `http://aether-qwen:8080/v1` 与 `http://aether-embedding:8081/v1`；在 WSL 主机运行的 Exchange 可加载 `/home/baijin/.local/state/cyrene-aether-wsl-acceptance/evidence/models-host.env`，使用当前私有 bridge 地址。E5 输入应按用途包含 `query: ` 或 `passage: ` 前缀；endpoint 不会改写输入。Qwen 模型许可为 Apache-2.0，E5 为 MIT（[Qwen 模型](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF)、[E5 模型](https://huggingface.co/intfloat/multilingual-e5-small)）。运行 Playwright 命令前加载生成的环境文件：

`identity prepare` 会在 `local-product/` 下分别生成 Product 数据面、Product 控制、Product endpoint bootstrap、Navigator session、Work 附件和本地 Control 凭据，并写入仅所有者可读的严格 Navigator principal map。身份限定为 actor `aether-local-acceptance`、organization `cyrene-aether-local`、workspace `local`；附件 principal 为 actor-private 且只读。同时写入 `embedding-profile.json`，固定 E5 模型 revision/摘要、384 维向量和 `passage: `/`query: ` 前缀。命令只打印文件路径和 scope 名，不显示 token 值。它为本地静态 Bearer 验收准备配置，不设置生产 Directory/OIDC 身份。

`speech prepare` 会将带哈希固定的 `sherpa-onnx` CPU wheel 和运行包仅安装到任务状态目录，在那里解包固定版 `imageio-ffmpeg` wheel，并下载 sherpa 兼容的 SenseVoice int8 ONNX 模型、tokenizer 与官方 `zh.wav` fixture（不可变 revision `2365baeacb507f821a0c8120fcee3d484dba7a07`）。wheel、模型、tokenizer、音频、FFmpeg 二进制哈希和运行时版本记录在 `models/speech/manifest.json`。模型遵循 [FunASR Model Open Source License Agreement](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)，需保留来源/作者归属和模型名称。随包 FFmpeg 报告 GPLv3；这两项只用于本地验收状态，不进入 Cyrene release。

按固定候选仓库执行真实 provider 进程检查：

```bash
python3 scripts/aether_wsl_acceptance.py speech smoke \
  --plugin-source <plugins-tools>/plugins/providers/speech-sensevoice \
  --contracts-sdk <plugins-contracts>/sdk/python \
  --expected-plugin-sha <完整的 40 位提交 SHA> \
  --expected-contracts-sha <完整的 40 位提交 SHA>
```

命令会用正式 provider bootstrap 在临时 `127.0.0.1` DirectPluginRuntime 地址启动子进程，经类型化 gRPC client 发送 fixture、校验类型化响应，再停止子进程。它不会调用 Product route 或云端 endpoint。修复后的 provider candidate 将 language 规范化为 `zh` 且本地 CPU 推理成功，但 sample 转写和上游文档预期相差一个字符；provider transport/推理通过，识别质量仍需单独审核。当前回执记录 Plugins `96568cff65ddefed9ffe086127ebd6aa0d2ad3df` 与 Contracts `79a3984dd362c58b5d20fa8ce5ea972408148748`。

运行 Playwright 命令前加载生成的环境文件：

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

每行将测试输入、可观察结果、行为归属代码区域和证据编号关联。候选提交固定后，记录对应仓库的精确 SHA，并把预期代码区域替换为实际源文件路径。只有当 fixture、mock 或 fake host 替代被测能力或宿主时才标记为 `SIMULATED`。若 fixture 仅是实际模型/插件/产品执行的输入，执行仍可标记为 `REAL`，同时单独记录输入来源。组件级 `REAL` 结果不能代替更完整的产品流程验收。

| 阶段 / 验收项 | 输入或操作 | 必须观察到的结果 | 追踪实现位置 | 验收证据 |
|---|---|---|---|---|
| P0 / 隔离 | 运行 `doctor`，启动隔离数据库，检查 Compose 标签、网络和端口 | 确认 Ubuntu 24.04 WSL2 x64；只创建带任务标签的资源；数据库无主机端口；旧 Docker 状态未改变 | Workspace `scripts/aether_wsl_acceptance.py`、`tooling/aether-wsl-acceptance/compose.yaml`；部署工作流保护由对应仓库负责人实现 | `P0/isolation`、Docker 检查摘要和 `doctor` 输出 |
| P0 / 边界 | 查询本地运行栈的 endpoint/config 清单 | 验收调用只指向 loopback 或本地任务网络；不选择云 VM、ACA、生产入口或生产 Bot | Workspace profile/config；Navigator 本地 host 配置 | `P0/endpoint-boundary`、脱敏 endpoint 清单 |
| P0 / 本地模型服务 | 使用固定 smoke 输入调用本地 Qwen chat 和 E5 embeddings API | 返回真实 Qwen 回复及两个有限、归一化的 384 维向量；模型服务不发布主机端口 | Workspace 模型 harness；官方 Qwen GGUF 与 E5 ONNX 制品 | `P0/local-model-endpoints`、模型 revision/文件哈希和推理回执 |
| P1 / 记忆持久化 | 通过正式插件客户端保存记忆，重启插件后读取 | 重启后读取到相同范围的记录和来源信息 | Plugins memory provider 契约/客户端及 SQLite 或 pgvector 实现 | `P1/memory-restart`、请求/结果 ID 和数据库对账 |
| P1 / 数据隔离 | 在不同账号/会话/persona 范围写入相同文本并分别查询 | 各查询只返回其获授权范围内的记录 | Plugins memory provider；Navigator scope 绑定 | `P1/memory-isolation`、各范围结果计数 |
| P1 / 向量完整性 | 用固定本地 embedding 模型为中英查询和文档生成向量 | 保存真实非零向量，绑定模型版本/维度；测试语料最近邻排序正确 | Exchange embedding API；Plugins memory provider | `P1/embedding-retrieval`、模型清单、维度和排序 ID |
| P1 / Product E5 embedding route | 经认证的 Product Gateway 发送 passage/query 输入，并在服务重启后复验 | 读回持久化 scope 路由；真实 E5 响应保留模型身份和输入索引，返回与 profile 维度一致的有限非零向量 | Exchange Product Gateway route planner；model-api-connector 与 E5 service | `P1/exchange-product-embedding-route`；本身不证明记忆持久化或召回 |
| P1 / E5 记忆 provider 组件 | 使用本地 E5 adapter 和 plugin runtime 调用七个 memory RPC，重开 SQLite 后再读 | 真实 384 维 E5 向量有限且非零；store/get/recall/delete/prune/export/import 可用，记录在 SQLite 重开后仍可读取 | Navigator `DirectEmbeddingClient`；Plugins model connector 与 memory SQLite provider | `P1/memory-e5-directplugin-sqlite-component`；临时 host 组合和 Product/Work 授权是独立门禁 |
| P1 / pgvector provider 组件 | 使用确定性测试向量对隔离 PostgreSQL 服务运行 memory-pgvector provider 套件 | 七个 RPC 均跨越 typed runtime boundary；scope、migration、分页、取消和连接池恢复测试通过 | Plugins memory-pgvector provider 与任务隔离 PostgreSQL | `P1/memory-pgvector-integration-suite`；测试向量是 fixture，不能据此证明正式 PackageSpec 安装 |
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
| P4 / 图谱 provider 组件 | 写入节点/边，逐项独立读取，查询来源引用，重开 SQLite 后再次读取 | 图谱方法结果和带来源的记录在 SQLite 重开后仍存在且摘要一致 | Navigator `DirectKnowledgeGraphProvider`；Plugins knowledge-graph SQLite provider | `P4/graph-directplugin-sqlite-component`；认证 Work ACL 是独立门禁 |
| P4 / 知识包 | 发布/选择 Catalyst 知识包版本，再从聊天检索 | 结果带包版本和来源；未授权内容被过滤 | Catalyst package lineage；Plugins retrieval；Navigator chat consumer | `P4/knowledge-citation`、包摘要和结果引用 |
| P4 / 重复项合并 | 提交不同来源的等价实体并预览/应用合并 | 保留全部来源关系；操作可审计、可恢复且重复执行幂等 | Navigator 图谱维护；Plugins graph provider | `P4/graph-merge`、预览/应用报告和来源计数 |
| P5 / Dream 有界处理 | 对多页 fixture 执行维护并检查内存/进度 | 分页、检查点、可取消；重启恢复且不重复变更 | Navigator DSH maintenance task；Plugins 分页存储 | `P5/dream-recovery`、页/检查点计数和重启报告 |
| P5 / Dream 变更控制 | 预览并应用合并、冲突、过期和删除批次 | 每项变化有来源/版本说明、当前授权检查、审计和恢复方式 | Navigator dream planner/apply；Plugins 存储 | `P5/dream-audit-restore`、计划摘要、审计项和恢复回读 |
| P6 / 研究来源 | 搜索、分页读取公开文档并引用选中内容 | 返回真实、受限、有来源标识的材料，并与私聊日志隔离 | Plugins public-web/document tools；Navigator DSH research tasks | `P6/research-provenance`、来源 URL、分页游标和内容摘要 |
| P6 / 搜索后端组件 | 向受限本地搜索后端发起两页查询并检查结果来源 | 按配置的 engine allowlist 返回真实 JSON 结果、来源 URL 和分页边界 | 任务专属 SearXNG 服务与隔离 search adapter | `P6/searxng-live-json-pagination`；只证明后端组件，不证明 Navigator/DSH 任务 |
| P6 / 后台取消 | 启动慢速读取后取消，再让 provider 返回迟到结果 | 任务保持取消状态；迟到结果不能注入上下文或发布 | Navigator task runner 和 tool adapter | `P6/cancel-late-result`、任务状态和未发生上下文变更的证明 |
| P6 / 工作笔记 | 分别保存工作笔记与待审核操作指南 | 笔记保留来源和信任状态；待审指南不会自动成为可信事实 | Navigator work-document/task state；Client 审核 | `P6/work-note-trust`、记录类型和读取回执 |
| P7 / Nous 交接 | 创建承诺、等待条件和活跃任务后交接/重启会话 | 持久交接恢复未完成工作和未知外部结果，不自动重放 | Navigator DSH session/task/schedule；Client 任务状态 | `P7/nous-handoff`、任务 ID 和前后状态 |
| P7 / 主动行为策略 | 触发跟进，再在发送前关闭订阅或进入安静时段 | 策略变更后不发送；发送时检查当前授权 | Navigator proactive behavior/send gate；connector receipt | `P7/proactive-send-gate`、策略回读和 connector 结果 |
| P7 / 群消息卫生 | 输入突发、撤回、自发及循环形状事件 | 防抖、合并、撤回和循环规则产生预期动作，不重复回复 | Navigator 输入/输出策略；消息 connector | `P7/group-hygiene`、事件到动作账本 |
| P8 / 语音 | 通过本地 provider 进程和类型化 DirectPluginRuntime client 发送不可变上游 `zh.wav` fixture | 返回真实非空转写、语言/状态、时长和分段；类型化错误明确。当前 fixture 结果与文档预期相差一个字符，质量审核仍未完成 | Plugins `speech-sensevoice` provider；任务专属 sherpa-onnx CPU runtime；Navigator 附件适配器是独立门禁 | `P8/asr-real`、Plugins/Contracts 精确 SHA、模型/token/音频与 FFmpeg 哈希、类型化回执 |
| P8 / 视觉模型组件 | 通过真实本地 vision model connector 和 DirectPlugin 直接发送两张生成的 PNG 输入 | 两项输入均得到模型响应；记录输入是合成 fixture | Exchange model connector；任务专属 llama.cpp 和固定 SmolVLM 权重 | `P8/vision-model-inference`、模型/connector 哈希和输入来源；不能通过附件或授权链路门禁 |
| P8 / 图片理解 | 把真实测试图片附加到消息并请求有依据的描述 | 模型结果能指向附件来源；不支持的媒体明确失败 | Exchange multimodal route；Navigator attachment context | `P8/image-real`、附件摘要和回复引用 |
| P8 / 媒体目录 | 添加、搜索、停用、恢复一张获授权表情/图片 | 搜索遵守账号/persona 范围；生成/保存与发送分离 | Plugins media provider/tool；Client media UI | `P8/media-lifecycle`、对象 ID 和状态回读 |
| P8 / 卡片生成 | 生成卡片并检查制品，再通过本地测试 connector 单独发送 | 制品摘要/版本稳定；发送回执是独立结果 | Plugins media tool；Navigator artifact/send APIs | `P8/card-and-send`、制品摘要和独立发送回执 |
| P9 / QQ | 用自有 QQ Connector 和获批测试 Host/账号登录并往返消息 | Host/账号状态、私聊/群聊消息和平台回读对账 | Plugins QQ connector/runtime；Navigator connector binding；Client 状态 | `P9/qq-real`；用户配置专用账号前为 `DEFERRED_BY_USER / NOT_RUN` |
| P9 / B站 | 搜索/读取后，通过获批测试账号执行评论/私信并回读 | 外部写入有持久意图/结果和独立平台回读 | Plugins Bilibili connector；Exchange 账号授权；Navigator task/receipt | `P9/bilibili-real`；用户配置专用账号前为 `DEFERRED_BY_USER / NOT_RUN` |
| P10 / 备份恢复 | 备份本地任务数据，在新的隔离位置恢复并对账 | 恢复后的记忆、画像、图谱和任务状态与清单一致，不导入密钥 | Workspace 运维手册；Navigator/Plugins 备份 adapter | `P10/backup-restore`、清单摘要和记录/任务计数 |
| P10 / 离线导入 | 对选定导出试运行、重复导入、撤销来源并回滚批次 | 映射/计数明确；未授权数据隔离；重试幂等；批次回滚有界 | Navigator 导入命令；Plugins provider 导入/导出；Client 诊断 | `P10/offline-import`、试运行及对账报告 |
| P10 / 每日复盘 | 对有界时间窗执行复盘并查看报告 | 报告标明证据缺口和可信来源；不会自动把私聊日志送入 Catalyst | Navigator review task；Catalyst 仅处理用户选定来源；Client 报告页面 | `P10/daily-review`、输入时间窗和报告来源清单 |

## 证据规则与当前状态

把验收产物放在 `/home/baijin/.local/state/cyrene-aether-wsl-acceptance/evidence/`。优先保存小型 JSON 回执、API 回读、脱敏浏览器结果、源码摘要和计数对账。不要保存原始授权头、凭据、私聊全文或未脱敏浏览器存储。`record` 命令只保存证据路径、SHA-256、候选提交和分类，不会打印证据内容。`PASS` 或 `FAIL` 必须引用至少一个归当前用户所有、普通文件、非符号链接、扩展名受限且大小有界的证据文件；gate 名称只接受小写 slug。`NOT_RUN` 可以只记录必需原因，不要求证据文件。

当前 WSL 基础设施门禁为 `PASS / REAL`：隔离 PostgreSQL、任务专用 Node/pnpm、Playwright Chromium 启动以及本地 Qwen/E5 推理。SenseVoice provider 子门禁也通过真实候选插件进程调用，但 sample 转写和上游预期相差一个字符。这些组件检查不能证明 Navigator 附件到上下文链路或完整 P8 产品验收；P1–P10 产品行为仍须等固定实现候选通过对应门禁后才能验收。用户稍后配置专用账号前，Teams、QQ 和 Bilibili 外部流量记为 `DEFERRED_BY_USER / NOT_RUN`。

首轮模型 connector 集成回执记录了候选 model-api-connector 与 Exchange gateway 对本地 Qwen/E5 的真实调用及 HTTP 200。该轮的 route planner、测试 principal 和 audit observer 使用模拟实现，因此持久化 Product route、生产身份、持久审计和固定 `18682` 服务仍为 `NOT_RUN / ACCEPTANCE_PENDING`。回执及拆分后的 `REAL` / `SIMULATED` 分类记录在 `model-api-connector-exchange-5afc0f4.json` 与 `evidence.jsonl`。

### WSL 实时验收证据更新（回执时间截至 2026-11-01 UTC）

本快照仅对下列明确列出的验收项更新前文的待验收说明。本地 Exchange 最初使用 scoped-route 候选 `5d5c68d30b353a15d486e639cbb3a7e7745f0d51`、model connector 源码 `b700c61deff4382e8e47ad67d22abc91b28da798` 和 SDK 候选 `8e271a0942ade7d5d7a197c79e71e500da7f6bf7`。通过 route-draft/confirm API 创建并持久化了 Product endpoint 与两条带 scope 的路由。认证后的模型列表和 Qwen chat 均返回 HTTP 200（`PASS / REAL`，`exchange-product-chat-5d5c68d.json`）。该 Exchange 候选的 `/v1/embeddings` 返回 HTTP 404（`FAIL / REAL`，`exchange-product-embedding-5d5c68d-fail.json`）。随后仅将本地 Exchange 服务切换到候选 `48c272304e6334e243317c044f4a228a8316103a`，保留原 SQLite 状态；`/readyz` 与持久模型列表读回均为 HTTP 200。认证的 Product embedding 请求返回 `intfloat/multilingual-e5-small`、有序 indices `[0,1]` 和两个有限非零 384 维单位向量（`PASS / REAL`，`exchange-product-e5-embeddings-48c2723.json`）。旧 404 作为前序候选回归回执保留；当前 Exchange Product embeddings 门禁已通过，完整记忆召回仍是独立检查。

另一路组件验收中，Navigator `DirectEmbeddingClient` 经 model connector 调用真实本地 E5，memory SQLite provider 的七个 RPC 全部通过且重开数据库后可读回（`PASS / REAL`，`navigator-adapter-real-direct-plugin-sqlite.json`）。Graph SQLite provider 的 upsert、逐项 node/edge 读取、sourceRefs 查询及重开后读取也通过（`PASS / REAL`，同一回执）。精确 adapter SHA 为 `b8229c45af4efe0a400ea68d8b79112388868846`，storage provider 候选为 `01776eadcd8309f39b2853697ad9ea166533adda`，model connector 为 `b700c61deff4382e8e47ad67d22abc91b28da798`，Plugins SDK 为 `f96b28c3e10233526f78b1355b302732a7aa1b31`。测试使用临时 loopback host 组合与临时 SQLite 数据库，receipt 将 host 组合标为 simulated，结束后删除临时数据。它不能证明 Navigator persistence 启动工厂、认证 Work 来源/群组 ACL、浏览器产品流程或官方 PackageSpec 安装。Product E5 embeddings 已由上方独立 Exchange 回执通过；经该持久路由的 P1 完整记忆召回仍待验收。

隔离 SearXNG 后端通过明确限制为 Wikipedia 的 engine allowlist，在两页结果中返回真实 JSON 搜索结果（`PASS / REAL`，`searxng-live-json-pagination.json`）。PublicWeb DirectPlugin 也通过该后端完成实时搜索/打开，并在子进程中解析本地 TXT fixture（`PASS / SIMULATED`，`public-web-searxng-real-and-parser-0ae782b.json`）：调用者是本地 harness，不是 Navigator/DSH 产品链路；文档输入是 fixture。这些组件回执不能通过 P6 产品任务、授权或附件桥接门禁。

固定的 SmolVLM 256M CPU 模型通过真实 model connector 和 DirectPlugin 为生成的图像输入推理并返回描述（`PASS / REAL`，`smolvlm-256m-real-model-inference-fixture-input.json`；详细回执为 `smolvlm-256m-real-image-inference.json`）。输入是合成 fixture，模型执行是真实的。这只证明本地图片推理，不证明用户附件已到达 Navigator/Exchange，也不证明产品授权检查。任务 Compose 健康探针最初指向错误端口；改为检查服务 `8082/health` 后容器已恢复健康（`smolvlm-compose-health.json`）。SenseVoice bootstrap/类型化 gRPC 推理在 provider 组件边界仍为 `PASS / REAL`；官方样例转写与文档预期相差一个字符（`speech-sensevoice-real.json`）。

PostgreSQL provider 测试套件在隔离数据库上完成 17 项测试（`database-test-20261101T003639Z-be7b31da.log`，Plugins 源码候选 `47784d422442b7353250b9e59bebf4a9d8d40a71`）。该次运行使用源码组合的 provider runtime 和 host 提供的确定性向量；它不能证明官方 PackageSpec 安装或由 E5 驱动的 Product 记忆检索。Product E5 embedding endpoint 已单独通过，但仍需验证经持久 Exchange route 的记忆持久化和召回。此快照中 Navigator/Client Web turn 和浏览器验收尚未运行。Teams、QQ 与 Bilibili 仍为 `DEFERRED_BY_USER / NOT_RUN`；云端 VM、ACA、生产入口及生产 Bot 配置不属于本轮测试目标，保持未改动。

按以下分类记录：

| 状态 | 模式 | 含义 |
|---|---|---|
| `PASS` 或 `FAIL` | `REAL` | 通过候选产品/插件执行，并使用隔离本地服务或获批真实 endpoint |
| `PASS` 或 `FAIL` | `SIMULATED` | fixture、mock provider 或 fake host 替代了正在测试的能力/宿主，因此不能证明该行为。仅作为真实执行输入的 fixture 应单独记录，执行仍可能是 `REAL` |
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
