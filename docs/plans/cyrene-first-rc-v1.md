# Cyrene First Usable RC V1 / 首个可用 RC

> **中文主计划 / Primary plan:** 把 Cyrene 从基础设施基线推进为个人开发者与研究者可用的首个 RC：安装、导入模型、清洗数据、单卡 SFT + LoRA、vLLM 部署、Exchange 网关与 API Key、真实客户端调用。
>
> **English summary:** First usable RC for single-user Linux NVIDIA hosts: browser console, model import, dataset curation, single-GPU SFT+LoRA, vLLM serving, Exchange gateway with API keys, and external OpenAI-compatible clients.

## 0. Document control / 文档控制

| Field | Value |
| --- | --- |
| Plan ID | CYRENE-FIRST-RC-V1 |
| Current status | IMPLEMENTING — Wave 0 baseline and Wave 2 import/gateway API foundations landed with local evidence; real GPU, HTTPS, installer and WebUI gates remain pending |
| Supported environment | Ubuntu 24.04 x86_64, one NVIDIA GPU (>= 12 GiB VRAM), browser local or remote |
| Acceptance model | `Qwen/Qwen2.5-1.5B-Instruct@5fee7c4ed634dc66c6e318c8ac2897b8b9154536` (Apache-2.0) |
| Release lock | [`release-lock.json`](../../release-lock.json) |
| Evidence record | [2026-09-20 Wave 0 / Wave 2 evidence](evidence/2026-09-20-rc-wave0-wave2.md) |
| RC exclusions | WinUI feature parity, team RBAC, multi-node scheduling, Echo feedback retraining, QLoRA, full-parameter training, multimodal |

Real acceptance (ACTUAL_PASS) requires the declared real environment: a native
Linux NVIDIA host, real model files, a real vLLM process, and a real second
device. Unit tests, fake executors, and HTTP 200 responses never produce
ACTUAL_PASS.

## 1. Status convention / 状态约定

Per the workspace status rule, every check starts `[ ]`. This document records
`CODE_COMPLETE` and `LOCAL_TEST_PASS` deltas without ticking the checkbox, and
only an Actual PASS with full evidence ticks `[x]`.

## 2. Wave 0 — Delivery baseline

- [ ] **RC-W0-01 — Web console package restored to Plugins.**
  `Cyrene-Plugins-Official/plugins/ui/navigator` now exists with README and
  `PROVENANCE.md` recording that the public history only contains a vanilla-TS
  probe shell and that the RC rebuild is React + TypeScript + Vite. Full console
  implementation remains Wave 1/2 work.
- [ ] **RC-W0-02 — Runtime capability records completed.**
  `training.llama-factory.v1` and `execution.engine.v1` both have manifests,
  owner-scoped schemas, TCK suites, provenance files, catalog entries, and
  public-CI coverage. Real CUDA/vLLM acceptance remains Wave 2/3.
- [ ] **RC-W0-03 — Developer stack drift repaired.**
  `cyrene-dev.py` no longer exports a removed Reactor worktree; the reference
  runtime no longer advertises a reactor runtime config it never writes; the
  service supervisor starts the DirectPlugin endpoint supervisor first and
  injects fresh connection references; legacy test/monkeypatch references are
  removed.
- [ ] **RC-W0-04 — Unified release lock.** [`release-lock.json`](../../release-lock.json)
  records the supported environment, the acceptance model, engine pins,
  plugin packages, and repository revisions. `scripts/validate_release_lock.py`
  validates it and names the open engine blockers.
- [ ] **RC-W0-05 — Acceptance model pinned.**
  `Qwen/Qwen2.5-1.5B-Instruct` at the exact revision is locked in the release
  lock for Wave 2/3 acceptance.
- [ ] **RC-W0-GATE — Development stack boots.** All services and required
  plugins healthy and the WebUI reachable without hand-injected connection
  references. **Pending:** requires the Wave 1 WebUI and a full `cyrene-dev up`
  run on the target host after the current repository changes are committed.

## 3. Wave 1 — Install, HTTPS, and credential foundation

- [ ] **RC-W1-01 — `.deb` and `cyrene` management command** (`init/up/down/status/doctor/logs/model import/backup/restore/upgrade/uninstall`).
- [ ] **RC-W1-02 — Installer preflight** for NVIDIA, disk, systemd, ports, CUDA compatibility.
- [ ] **RC-W1-03 — Workspace/database/artifact/credential initialization** and automatic required-plugin installation.
- [ ] **RC-W1-04 — Local loopback HTTP vs remote HTTPS mode** with Caddy or an existing reverse proxy.
- [ ] **RC-W1-05 — One-time pairing code and revocable Secure/HttpOnly/SameSite cookie sessions.**
- [ ] **RC-W1-06 — Platform-owned Credential Store** where HF tokens are write-only, encrypted at rest, resolved by `CredentialRef`, and never echoed.
- [ ] **RC-W1-GATE — Clean-host install** and remote HTTPS pairing/revocation.

## 4. Wave 2 — Model import, deployment, and gateway

- [ ] **RC-W2-01 — Reactor persisted ModelImport.**
  Implemented: `POST/GET /api/v1/model-imports`, HF repo + pinned revision,
  `CredentialRef`, absolute local path, validation evidence, `trust_remote_code`
  refusal, idempotent replay, persisted failure. Local tests pass.
- [ ] **RC-W2-02 — vLLM model import.**
  Implemented: `POST /imports` validates weights/config/tokenizer/chat template,
  resolves private credential refs from mode-0600 files, publishes a portable
  `model` artifact, and refuses remote code. Local tests and TCK pass.
- [ ] **RC-W2-03 — Real deployment path.**
  Reactor forwards artifacts to the vLLM runtime and requires identity read-back
  plus a live inference probe before READY. **Pending:** real vLLM/GPU run.
- [ ] **RC-W2-04 — Exchange resource lists.**
  Implemented: `GET /api/v1/gateway-endpoints` and `GET /api/v1/gateway-routes`.
- [ ] **RC-W2-05 — Exchange API keys.**
  Implemented: server-generated `cyk_…` secrets shown once, SHA-256 digest
  storage, name/expiry/scope, list/get/revoke, workspace isolation, idempotent
  replay without secret. CLI `key issue/list/revoke`.
- [ ] **RC-W2-06 — `/v1` gateway contract.**
  Implemented: `/v1/models` authenticates with the gateway credential and
  filters by model scope; `/v1/chat/completions` rejects out-of-scope models with
  `model_not_permitted` and audits the rejection; unary + SSE unchanged.
- [ ] **RC-W2-07 — Deployment events/observation API.** **Pending.**
- [ ] **RC-W2-08 — WebUI "deploy and publish API" wizard.** **Pending** (Wave 1 WebUI).
- [ ] **RC-W2-GATE — Real GPU path from import to streaming chat, stop, and restart.**

## 5. Wave 3 — Data and real SFT + LoRA

Catalyst: JSON/JSONL/CSV/Parquet/text import, instruction and conversation
mapping, preview, error rows, dedupe, normalization, deterministic split with a
recorded seed, and immutable DatasetVersion with quality report.

Yield: training draft/run/list/events/SSE/resume APIs, LLaMA Factory YAML
import/export with strict unknown-field errors, canonical TrainingSpec parameter
surface, checkpoint-backed resume rules, and preflight covering GPU, VRAM, CUDA,
model, dataset, disk, runtime, and write permission.

- [ ] **RC-W3-GATE — Real CUDA SFT + LoRA** from DatasetVersion to a PEFT adapter
  with loss, checkpoint, cancel, resume, and GPU release evidence.

## 6. Wave 4 — End-to-end delivery

- [ ] **RC-W4-01 — `BASE_PLUS_LORA` ModelVersion** with base artifact, adapter,
  canonical `adapter_config.json`, tokenizer/template inheritance, and full lineage.
- [ ] **RC-W4-02 — Send to Reactor** creates an auditable Deployment Draft;
  adapter-aware probe proves the adapter is loaded.
- [ ] **RC-W4-03 — Send to Exchange** creates an editable Route Draft; activation
  requires explicit confirmation.
- [ ] **RC-W4-04 — Gateway page** shows base URL, model ID, API key, and
  curl/Python/JavaScript snippets plus "Use in Navigator".
- [ ] **RC-W4-05 — Navigator conversation surface** bound to the Exchange route;
  a missing session surface blocks the RC.
- [ ] **RC-W4-06 — Exports** for LLaMA Factory YAML, HF/PEFT adapter package, and
  deployment manifest; no merged full weights in RC.
- [ ] **RC-W4-GATE — DatasetVersion → training → deployment → API → Navigator and
  external client** with real replies.

## 7. Public interfaces added by this plan

| Product | Interface |
| --- | --- |
| Navigator / Platform | `POST /api/v1/auth/pair`, `GET/DELETE /api/v1/auth/session`, `GET /api/v1/system/status`, credential metadata create/list/revoke, fixed forwarding prefixes, CSRF, tracing, idempotency propagation |
| Yield | `GET /api/v1/training-runs`, preflight/events/events-stream/resume actions, LLaMA Factory draft import/export |
| Reactor | ModelImport create/list/get, deployment events/observation |
| Exchange | endpoint/route lists, API keys, `/v1/models`, `/v1/chat/completions` |

All new errors use RFC 9457; mutations support idempotency keys and conflict
detection.

## 8. Test and release standard

- Automated: contracts/schemas, storage migration, idempotency, pagination,
  errors, restart recovery per Product; plugin manifest/entrypoint/DirectPlugin
  runtime/TCK/package acceptance; WebUI component/a11y/E2E; pairing expiry,
  cookie/CSRF, API-key revocation, HF token non-echo, log redaction, path
  traversal; install/upgrade/rollback/backup/restore/uninstall.
- Real environment: public and private HF import, 512-sample SFT+LoRA, metrics,
  checkpoint, cancel, resume, GPU release, adapter-aware deployment, streaming
  and revocation, second-device HTTPS, host restart consistency, upgrade without
  data loss.
- External matrix: Open WebUI, Cherry Studio, Continue against `/v1/models` and
  streaming chat.
- Delivery: capability contracts first, then Product APIs, then plugins, WebUI,
  and installer; each repository passes its exact-SHA local gate and hosted CI
  before `develop`; the Workspace locks immutable revisions for the full
  GPU/HTTPS/external acceptance and only then publishes `0.1.0-rc.1`.

## 9. After RC

RC.2 adds Echo evaluation and comparison; V1 closes feedback into Catalyst;
later milestones add WinUI, outbound relay, multi-user RBAC, multi-node
scheduling, QLoRA/full training, multimodal, and a plugin market.
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene 首个可用 RC V1

> **中文主计划**：将 Cyrene 从基础设施基线推进为个人开发者与研究者可用的首个 RC：安装、模型导入、数据清理、单卡 SFT + LoRA、vLLM 部署、Exchange 网关与 API Key，以及真实客户端调用。
>
> **英文摘要**：面向单用户 Linux NVIDIA 主机的首个可用 RC，提供浏览器控制台、模型导入、数据集整理、单 GPU SFT+LoRA、vLLM 服务、带 API Key 的 Exchange 网关，以及外部 OpenAI 兼容客户端接入。

## 0. 文档控制

| 字段 | 值 |
|---|---|
| 计划 ID | CYRENE-FIRST-RC-V1 |
| 当前状态 | 正在实施 — Wave 0 基线和 Wave 2 导入 / 网关 API 基础已落地并有本地证据；真实 GPU、HTTPS、安装程序和 WebUI 门禁仍待完成 |
| 支持环境 | Ubuntu 24.04 x86_64，单块 NVIDIA GPU（显存至少 12 GiB），浏览器可在本地或远程 |
| 验收模型 | `Qwen/Qwen2.5-1.5B-Instruct@5fee7c4ed634dc66c6e318c8ac2897b8b9154536`（Apache-2.0）|
| 发布锁定文件 | [`release-lock.json`](../../release-lock.json) |
| 证据记录 | [2026-09-20 Wave 0 / Wave 2 证据](evidence/2026-09-20-rc-wave0-wave2.md) |
| RC 排除项 | WinUI 功能对等、团队 RBAC、多节点调度、Echo 反馈再训练、QLoRA、全参数训练、多模态 |

真实验收（`ACTUAL_PASS`）必须在声明的真实环境中完成：原生 Linux NVIDIA 主机、真实模型文件、真实 vLLM 进程以及第二台真实设备。单元测试、fake executor 和 HTTP 200 响应都不能产生 `ACTUAL_PASS`。

## 1. 状态约定

根据 Workspace 状态规则，每项检查初始均为 `[ ]`。本文记录 `CODE_COMPLETE` 和 `LOCAL_TEST_PASS` 的变化，但不勾选复选框；只有取得完整证据的 Actual PASS 才标记 `[x]`。

## 2. Wave 0 — 交付基线

- [ ] **RC-W0-01 — 将 Web 控制台包恢复到 Plugins。**
  `Cyrene-Plugins-Official/plugins/ui/navigator` 现已存在，并包含 README 和 `PROVENANCE.md`；该文件说明公开历史中只有一个原生 TS 探测 shell，RC 重建版本则采用 React + TypeScript + Vite。完整控制台实现仍属于 Wave 1/2 工作。
- [ ] **RC-W0-02 — 完成运行时能力记录。**
  `training.llama-factory.v1` 和 `execution.engine.v1` 均已具备 manifest、owner 作用域 schema、TCK 套件、来源文件、目录条目和 public CI 覆盖。真实 CUDA/vLLM 验收仍属于 Wave 2/3。
- [ ] **RC-W0-03 — 修复开发技术栈漂移。**
  `cyrene-dev.py` 不再导出已移除的 Reactor 工作树；参考运行时不再声明它实际上不会生成的 Reactor runtime config；服务监督器优先启动 DirectPlugin endpoint 监督器并注入新的连接引用；已移除旧测试 / monkeypatch 引用。
- [ ] **RC-W0-04 — 统一发布锁定文件。**[`release-lock.json`](../../release-lock.json) 记录支持环境、验收模型、引擎固定版本、插件包和仓库修订版。`scripts/validate_release_lock.py` 会验证该文件并指出尚未解决的引擎阻塞项。
- [ ] **RC-W0-05 — 固定验收模型。**
  在 Wave 2/3 验收中，`Qwen/Qwen2.5-1.5B-Instruct` 的精确修订版已锁定到 release lock。
- [ ] **RC-W0-GATE — 开发栈启动。**所有服务和必需插件均健康，且 WebUI 无需人工注入连接引用即可访问。**待完成**：需先有 Wave 1 WebUI，并在提交当前仓库变更后于目标主机上完整运行一次 `cyrene-dev up`。

## 3. Wave 1 — 安装、HTTPS 与凭据基础

- [ ] **RC-W1-01 — `.deb` 和 `cyrene` 管理命令**（`init/up/down/status/doctor/logs/model import/backup/restore/upgrade/uninstall`）。
- [ ] **RC-W1-02 — 安装器预检**：检查 NVIDIA、磁盘、systemd、端口和 CUDA 兼容性。
- [ ] **RC-W1-03 — 初始化 Workspace / 数据库 / 制品 / 凭据**，并自动安装必需插件。
- [ ] **RC-W1-04 — 本地 loopback HTTP 与远程 HTTPS 模式**，通过 Caddy 或现有反向代理提供。
- [ ] **RC-W1-05 — 一次性配对码，以及可撤销的 Secure/HttpOnly/SameSite Cookie 会话。**
- [ ] **RC-W1-06 — Platform 所有的 Credential Store**：HF token 仅可写入、静态加密、通过 `CredentialRef` 解析且不会回显。
- [ ] **RC-W1-GATE — 干净主机安装**，并完成远程 HTTPS 配对 / 撤销。

## 4. Wave 2 — 模型导入、部署与网关

- [ ] **RC-W2-01 — Reactor 持久化 ModelImport。**
  已实现：`POST/GET /api/v1/model-imports`、HF repo + 固定修订版、`CredentialRef`、绝对本地路径、验证证据、拒绝 `trust_remote_code`、幂等重放和持久化失败状态。本地测试通过。
- [ ] **RC-W2-02 — vLLM 模型导入。**
  已实现：`POST /imports` 校验权重 / config / tokenizer / chat template；从权限为 mode-0600 的文件解析私有凭据引用；发布可移植的 `model` 制品；拒绝远程代码。本地测试和 TCK 通过。
- [ ] **RC-W2-03 — 真实部署路径。**
  Reactor 将制品转发给 vLLM runtime，并要求完成身份回读和在线推理探测后才进入 `READY`。**待完成**：真实 vLLM/GPU 运行。
- [ ] **RC-W2-04 — Exchange 资源列表。**
  已实现：`GET /api/v1/gateway-endpoints` 和 `GET /api/v1/gateway-routes`。
- [ ] **RC-W2-05 — Exchange API Key。**
  已实现：服务端生成 `cyk_…` 秘密且只显示一次；以 SHA-256 摘要存储；支持名称 / 到期时间 / 范围、列表 / 获取 / 撤销、Workspace 隔离以及不返回秘密的幂等重放。CLI 提供 `key issue/list/revoke`。
- [ ] **RC-W2-06 — `/v1` 网关合约。**
  已实现：`/v1/models` 使用网关凭据认证，并按模型范围过滤；`/v1/chat/completions` 对超出范围的模型返回 `model_not_permitted` 并审计拒绝；一元调用与 SSE 行为不变。
- [ ] **RC-W2-07 — 部署事件 / 观测 API。** **待完成。**
- [ ] **RC-W2-08 — WebUI“部署并发布 API”向导。** **待完成**（依赖 Wave 1 WebUI）。
- [ ] **RC-W2-GATE — 从导入到流式聊天、停止和重启的真实 GPU 路径。**

## 5. Wave 3 — 数据与真实 SFT + LoRA

**Catalyst**：导入 JSON/JSONL/CSV/Parquet/文本；支持 instruction 和 conversation 映射、预览、错误行、去重、规范化、带记录 seed 的确定性切分；生成带质量报告的不可变 `DatasetVersion`。

**Yield**：提供 training draft/run/list/events/SSE/resume API；支持 LLaMA Factory YAML 导入 / 导出并严格报告未知字段错误；提供规范 `TrainingSpec` 参数界面、基于 checkpoint 的恢复规则；预检 GPU、VRAM、CUDA、模型、数据集、磁盘、runtime 和写入权限。

- [ ] **RC-W3-GATE — 真实 CUDA SFT + LoRA**：从 `DatasetVersion` 生成 PEFT adapter，并提供 loss、checkpoint、取消、恢复和 GPU 释放证据。

## 6. Wave 4 — 端到端交付

- [ ] **RC-W4-01 — `BASE_PLUS_LORA` ModelVersion**：包括基础制品、adapter、规范 `adapter_config.json`、tokenizer/template 继承和完整血缘。
- [ ] **RC-W4-02 — 发送至 Reactor**，生成可审计的 Deployment Draft；adapter 感知探测必须证明 adapter 已加载。
- [ ] **RC-W4-03 — 发送至 Exchange**，生成可编辑的 Route Draft；激活前须明确确认。
- [ ] **RC-W4-04 — 网关页面**显示 base URL、模型 ID、API Key、curl/Python/JavaScript 代码示例以及“在 Navigator 中使用”。
- [ ] **RC-W4-05 — Navigator 对话界面**绑定 Exchange route；缺少会话界面将阻塞 RC。
- [ ] **RC-W4-06 — 导出** LLaMA Factory YAML、HF/PEFT adapter 包和 deployment manifest；RC 中不包含合并后的完整权重。
- [ ] **RC-W4-GATE —** 真实回复贯通 `DatasetVersion → training → deployment → API → Navigator` 和外部客户端。

## 7. 本计划新增的公共接口

| Product | 接口 |
|---|---|
| Navigator / Platform | `POST /api/v1/auth/pair`、`GET/DELETE /api/v1/auth/session`、`GET /api/v1/system/status`、credential metadata create/list/revoke、固定转发前缀、CSRF、trace 和幂等传播 |
| Yield | `GET /api/v1/training-runs`、preflight/events/events-stream/resume 操作、LLaMA Factory draft 导入 / 导出 |
| Reactor | ModelImport create/list/get、deployment events/observation |
| Exchange | endpoint/route 列表、API Key、`/v1/models`、`/v1/chat/completions` |

所有新增错误使用 RFC 9457；所有变更操作支持幂等键和冲突检测。

## 8. 测试与发布标准

- **自动化验证**：合约 / schema、存储迁移、幂等、分页、错误处理和每个 Product 的重启恢复；插件 manifest/entrypoint/DirectPlugin runtime/TCK/包验收；WebUI 组件 / 可访问性 / E2E；配对过期、Cookie/CSRF、API Key 撤销、HF token 不回显、日志脱敏、路径遍历；安装 / 升级 / 回滚 / 备份 / 恢复 / 卸载。
- **真实环境验收**：公开与私有 HF 导入、512 样本 SFT+LoRA、指标、checkpoint、取消、恢复、GPU 释放、adapter 感知部署、流式传输与撤销、第二设备 HTTPS、主机重启一致性和无数据丢失升级。
- **外部客户端矩阵**：使用 Open WebUI、Cherry Studio、Continue 测试 `/v1/models` 和流式聊天。
- **交付顺序**：先能力合约，再 Product API，然后插件、WebUI 和安装器；每个仓库在 `develop` 前通过其精确 SHA 的本地门禁和 hosted CI。Workspace 锁定不可变修订版，以完成完整 GPU/HTTPS/外部客户端验收，之后才发布 `0.1.0-rc.1`。

## 9. RC 之后

RC.2 增加 Echo 评估与比较；V1 将反馈闭环接入 Catalyst。后续里程碑增加 WinUI、出站 Relay、多用户 RBAC、多节点调度、QLoRA / 全参数训练、多模态和插件市场。
