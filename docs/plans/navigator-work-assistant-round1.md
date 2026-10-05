# Navigator work assistant migration, round 1

Status: **IMPLEMENTING; VM CUTOVER NOT PERFORMED**. This round creates a service baseline; source integration and real host acceptance have separate evidence.

Navigator's DeepSeek Harness is the sole top-level executor. Exchange serves ordinary model API requests. Antigravity and CodeBuddy retain their vendor login and native agent process, exposed as dsh subagents rather than Exchange model adapters. Workspace identity, durable tasks, approvals, receipts, memory freshness, attachments and notification deduplication remain Navigator-owned.

QQ and CLI run on the Navigator machine. The existing paired Client can connect to a Linux cloud or Windows local Navigator. This round does not introduce a remote host agent. Intended hosts are Linux x64/ARM64 and Windows x64; build evidence, credentialed operation and release publication are recorded independently.

WeCom's Python bot-plus-CLI package is the initial connector entry. Bot and authorized human CLI identities remain distinct. The QQ product is called **QQ connector**; OneBot stays a compatibility protocol. QQ packages come from authenticated official-source manifests without redistribution. A production QQ Host still requires the lawful documented interface and provenance evidence in [the existing source boundary](../../governance/qqnt-direct/CLEAN_ROOM_BOUNDARY.md). No NapCat dependency is permitted. A simulated Host cannot establish real QR login, private chat or group chat support.

Patrol requirements are versioned workflow memory: configured targets, permissions, evidence, freshness and notification rules. The pinned dsh Schedule service owns durable timing, with UTC as the default timezone. Unchanged patrol results remain quiet. Hugging Face MCP, Azure CLI/MCP plus Learn documentation, and gcloud plus Google documentation execute with credentials on their actual host; resource inspection defaults to read-only.

## Source baselines

| Repository | Fetched develop baseline | Retained local work |
| --- | --- | --- |
| Navigator | `04e84f7b7943bbe0d60604ff65194904b37ad821` | Executor/dsh upgrade through `8284282`; merged baseline in isolated checkout |
| Plugins | `96733779d187b13efcadcde95a6b6737fe102d25` | WeCom Python commit `bf810bb` replayed as `1390a31`; original Rust dirty work preserved |
| Client | `9c51a27540951589157926d1ee717d912ed22fc2` | Existing pairing and Control authority |
| Workspace | `599bbe02a8d8b4050042c75c23c297d33021b8ea` | Existing QQ source boundary and distribution authority |
| Exchange | `7f1823f6eabe9956ff40cc2e4aa9035197b51e83` | Existing model API owner; no second agent executor |

Delivery records must include ordinary PR merge, exact-SHA CI, canonical read-back, local test commands and unavailable real environments. See Navigator's `docs/operations/round1-acceptance.md` for execution evidence. These baselines are historical records, not a substitute for fetching current refs in later work.

## Acceptance and subsequent work

- [ ] NWA1-FLOW: simulated WeCom intake → durable task → real dsh loop/model request → human approval or result → durable notification receipt.
- [ ] NWA1-RECOVERY: failure, cancellation, restarted task queries and ordered event recovery.
- [ ] NWA1-CONNECTORS: WeCom reconnect and identity isolation; QQ Host protocol, QR state, confirmed account and error handling.
- [ ] NWA1-HOST: real Exchange, WeCom tenant, QQ login/private/group messages and target host builds; unavailable cases remain `NOT_RUN`.
- [ ] NWA1-DELIVERY: repository gates, ordinary PR merges and remote read-back.

The old service and snapshots remain intact. After this custom assistant migration, the old tree principally retains Hermes's own engine, gateway, model adapters, unmigrated generic tools, skills and compatibility code. Hermes general tools/skills and DH-Skills belong to the next round. Existing conversation, credential and notification databases are not copied wholesale. VM replacement requires explicit real-host acceptance and a separate reviewed data migration.

---
<!-- Chinese Translation / 中文翻译 -->

# Navigator 工作助手迁移第一轮

状态：**实施中；未切换线上 VM**。dsh 是唯一主执行核心；Exchange 负责普通模型 API，Antigravity、CodeBuddy 按厂商登录方式作为子代理运行。任务、审批、操作核验、来源与新鲜度、附件、通知去重统一由 Navigator 保存。

QQ 和 CLI 运行在 Navigator 所在机器；本地 Client 沿用配对与 Control，可连接云端或本地部署。本轮目标是 Linux x64/ARM64、Windows x64；源码合入、目标构建、真实账号运行和发布支持分别记录。企微以 Python 双能力插件作为入口，机器人和真人 CLI 身份分开。产品入口叫 QQ 连接器，保留 OneBot 兼容；禁止 NapCat，真实 QQ Host 仍遵循现有来源边界。

巡检目标、权限、证据和通知规则保存为版本化工作流记忆，由上游 dsh 持久 Schedule 触发，默认 UTC；无变化保持安静。HF MCP、Azure CLI/MCP 与 Learn 文档、gcloud 与 Google 文档按实际宿主管理凭据，资源巡检默认只读。

上表固定本轮起点，后续必须重新 fetch。验收记录应包含测试环境、普通 PR 合并、精确 SHA 的 CI 和远端读回；模拟链路不能替代真实企微、QQ、Exchange 或目标系统证据。旧服务及快照保留，线上替换和数据迁移另行验收。Hermes 自身引擎、网关、模型适配、未迁通用工具及兼容代码继续留在旧树；Hermes skills 和 DH-Skills 留到下一轮。
