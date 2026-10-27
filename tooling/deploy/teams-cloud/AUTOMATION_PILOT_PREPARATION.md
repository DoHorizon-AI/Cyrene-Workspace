# Automation pilot preparation / 自动化试点准备

This guide prepares the private configuration boundary for the read-only automation pilot. It does not activate a workflow, install a personal Bot, add a member, start a new provider process, change the current VM, or claim cloud acceptance. The default state is `CYRENE_WORKFLOWS_ENABLED=false`, `CYRENE_TEAMS_PERSONAL_CHAT_ENABLED=false`, and `personal_chat_enabled=false` with an empty personal-chat actor allowlist.

本指南准备只读自动化试点的私有配置边界，不会启用 workflow、安装个人 Bot、添加成员、启动新的 Provider 进程、修改当前 VM 或声称云端验收通过。默认状态为 `CYRENE_WORKFLOWS_ENABLED=false`、`CYRENE_TEAMS_PERSONAL_CHAT_ENABLED=false`，并将 `personal_chat_enabled=false`、个人聊天 actor allowlist 留空。

The repository provides the opt-in `compose.automation-workflows.yaml` overlay, schemas for the two host-private reference maps, and a placeholder-only Work-principal merge fragment. The root-owned `compose.automation-providers.yaml` overlay is separate and adds the Microsoft Graph and Azure DevOps provider processes. Keep both overlays out of the active selection until the private profiles, current ACL checks, package/image locks, and a passing capacity gate are present. Workspace preflight validates the optional actor/bridge secret boundary and fixed provider topology. This is configuration validation; it does not establish OAuth, source access, live delivery or capacity admission.

仓库提供可选的 `compose.automation-workflows.yaml` overlay，以及两个宿主机私有引用映射的 Schema。另一个由 root 负责的 `compose.automation-providers.yaml` overlay 才会添加 Microsoft Graph 和 Azure DevOps Provider 进程。在私有 profile、实时 ACL 校验、package/image 锁定及容量门禁通过前，不要把任一 overlay 加入当前服务选择。Workspace preflight 会校验可选 actor／bridge secret 边界和固定 Provider 拓扑。这属于配置验证，不能替代 OAuth、来源权限、真实投递或容量准入。

The stock CLI wires Planner through registered Graph tools with a fresh plan-read
probe. Product/training and sanitized host adapters have local tests and mandatory
authorization ports, but their production ACL/credential clients are not yet
implemented. Those sources remain disabled even when a selector file is present;
this preparation does not complete the four-source notification MVP.

当前 CLI 通过注册的 Graph 工具和即时计划读取探针接入 Planner。Product／训练及脱敏宿主
适配器已有本地测试和强制授权接口，但其生产 ACL／凭据客户端尚未实现；仅填写选择器
文件仍不会启用这些来源。因此本次准备不能算四类来源通知 MVP 已完成。

Personal transport has four matching host settings: Navigator's
`CYRENE_TEAMS_PERSONAL_CHAT_ENABLED`, the ingress `TEAMS_PERSONAL_CHAT_ENABLED`,
the Teams provider's `personal_chat_enabled`, and Exchange
`connectorHost.teamsMessages.personalActorSubjects`. Work also needs its fixed
`teams_private_conversations` policy and the current read-only actor projection.
The optional overlay wires the ingress flag and Work mount; preflight rejects a
partial or mismatched activation. A real personal installation is still required.

私聊传输需对齐上述四项宿主设置，以及 Work 的固定私聊策略和实时只读 actor 投影。
overlay 已接好 ingress 开关和 Work 挂载；预检拒绝不完整或不一致的启用配置，但配置
通过仍不能代替真人安装验证。

The existing CodeBuddy/model login is restricted by the separate explicit
`CYRENE_TEAMS_MODEL_ACTOR_SUBJECT` selector. The prepared pilot selects only the
approved owner; empty means no shared-model entitlement in multi-actor mode.
Adding a Directory member or receiving a notification does not grant model use.

现有 CodeBuddy／模型登录通过独立的 `CYRENE_TEAMS_MODEL_ACTOR_SUBJECT` 明确绑定给本人。
多人模式留空时不授予共享模型使用权；添加 Directory 成员或接收通知都不会继承该授权。

## Authority files and secret references / 授权文件与密钥引用

Use the existing private `.artifacts` configuration and secrets directories outside every source worktree. Do not commit populated versions or copy real tenant, actor, connection, plan, Product, host, pipeline, or release IDs into a template. The actor token map and source binding map contain private selectors, not credential values or grants.

使用现有位于所有源码工作树之外的私有 `.artifacts` 配置目录和 secrets 目录。不要提交已填充的文件，也不要把真实 tenant、actor、connection、plan、Product、host、pipeline 或 release ID 写入模板。actor token map 和 source binding map 仅保存私有选择器，不包含凭据值，也不授予访问权。

`CYRENE_WORK_ACTOR_TOKEN_MAP_FILE` points to `work-actor-token-map.json` in the read-only `/run/teams-authorization` mount. Its deployment-specific closed version-1 shape is documented by [`config/work-actor-token-map.schema.json`](config/work-actor-token-map.schema.json): one workspace ID and exactly five AAD actor IDs (the current owner plus four approved pilot actors), each referring to a unique fixed token environment name (`CYRENE_WORK_ACTOR_TOKEN_01` through `_05`). The runtime loader remains compatible with one-to-five actors for gradual rollout, but this multiuser deployment profile requires all five. The file contains names only. Each referenced secret is a distinct server-issued Work writer token, never the session token, executor token, private Teams bridge token, or another actor's token.

`CYRENE_WORK_ACTOR_TOKEN_MAP_FILE` 指向只读 `/run/teams-authorization` 挂载中的 `work-actor-token-map.json`。封闭的 v1 格式见 [`config/work-actor-token-map.schema.json`](config/work-actor-token-map.schema.json)：一个 Workspace ID、五个 AAD actor ID（owner 及四位试点成员）；运行时加载器为渐进启用保留一至五人兼容，每个 actor 引用唯一固定的 token 环境变量名（`CYRENE_WORK_ACTOR_TOKEN_01` 至 `_05`）。文件中只有变量名。每个引用的 secret 都是服务端为对应 Work writer 签发的独立 token，不得复用 session token、executor token、Teams private bridge token 或其他 actor 的 token。

For this Compose bundle, keep both map files owner-only (`UID 10001`, mode `0600`) inside the already-private `CYRENE_CONFIG_DIR` (directory mode `0700`). The runtime also supports a root-owned, current-service-group-readable `0640` file when its parent is root-owned; do not mix that alternate mode with the owner-only Compose config directory. The loader rejects symlinks, broad permissions, extra fields, duplicate actor/token references, invalid paths, and overlarge files.

本 Compose bundle 中，两份 map 都应放在已有私有 `CYRENE_CONFIG_DIR` 内，设为 UID `10001`、权限 `0600`；目录本身为 `0700`。运行时也支持 root 所有、当前服务组可读的 `0640` 文件，但其父目录必须同为 root 所有；不要把这一替代模式与 owner-only Compose 配置目录混用。加载器会拒绝符号链接、过宽权限、额外字段、重复 actor/token 引用、无效路径和超大文件。

`CYRENE_AUTOMATION_SOURCE_BINDINGS_FILE` points to `automation-source-bindings.json` in the same read-only mount. Its exact selectors are in [`config/automation-source-bindings.schema.json`](config/automation-source-bindings.schema.json). Rows are restricted to Yield `training_run`, other Product resources, `host_health`, or `planner_plan`. For `host_health`, set `mountDirectory` to `/run/cyrene/automation-health`; the overlay mounts the operator-provided `CYRENE_AUTOMATION_HEALTH_EXPORT_DIR` there read-only and refuses to create a missing host directory. The root-owned exporter must write `latest-status.json` in this directory. There are no arbitrary URLs, headers, credentials, or `accessVerified` fields. A binding selects a target; the Product Directory, host, or Planner source authorization client must approve that actor and target on every poll. Missing credentials, resources, configuration, or current authorization remain explicit `NOT_CONFIGURED`/forbidden states; an empty result is not a success substitute.

`CYRENE_AUTOMATION_SOURCE_BINDINGS_FILE` 指向同一只读挂载中的 `automation-source-bindings.json`。精确选择器见 [`config/automation-source-bindings.schema.json`](config/automation-source-bindings.schema.json)。行类型仅限 Yield `training_run`、其他 Product resource、`host_health` 或 `planner_plan`。`host_health` 的 `mountDirectory` 固定为 `/run/cyrene/automation-health`；overlay 将 operator 提供的 `CYRENE_AUTOMATION_HEALTH_EXPORT_DIR` 只读挂载到该位置，并拒绝自动创建缺失的宿主目录。root-owned exporter 必须在其中写入 `latest-status.json`。文件不接受任意 URL、header、凭据或 `accessVerified` 字段。binding 只选择 target；每次轮询仍须由 Product Directory、host 或 Planner source authorization client 对该 actor 和 target 作出当前授权决定。凭据、资源、配置或当前授权缺失时必须显示为明确的 `NOT_CONFIGURED`/forbidden 状态，不能用空结果冒充成功。

The overlay maps five separate owner-only files, `work-actor-token-01` through `work-actor-token-05`, to the token environment names above. It maps the distinct `teams-private-bridge-token` into Navigator and Navigator Persistence only. Store these files under `CYRENE_SECRETS_DIR`, owner-readable only; never place token bytes in Compose interpolation, JSON configuration, workflow inputs, logs, model context, or a child process environment. The existing secret-env entrypoint reads each mounted secret into only the fixed environment variable named by the overlay.

overlay 将五个独立 owner-only 文件 `work-actor-token-01` 至 `work-actor-token-05` 映射到上述 token 环境变量，并仅将单独的 `teams-private-bridge-token` 注入 Navigator 与 Navigator Persistence。文件应放在 `CYRENE_SECRETS_DIR` 下并只允许 owner 读取；token 字节不得进入 Compose 插值、JSON 配置、workflow 输入、日志、模型上下文或子进程环境。现有 secret-env entrypoint 只把挂载的 secret 载入 overlay 指定的固定环境变量。

The Work principal file separates these powers. The placeholder-only [`config/automation-work-principals.patch.template.json`](config/automation-work-principals.patch.template.json) defines exactly five actor writer rows: the owner at token slot 01 and four approved pilot actors at slots 02–05. Every writer needs `can_takeover=true` for private Work mutations, `can_write_harness=true` for actor-private Harness session persistence, and `actor_private_scope=true` as its data visibility boundary, with exactly one organization-scoped Workspace. The latter keeps Work access private to that actor; it does not replace the write grants. This file is a merge fragment, not a complete runtime principal config: append its five writers to the existing `principals` array, preserve the existing global `NAVIGATOR_SESSION_TOKEN`/`CYRENE_SESSION_TOKEN` and shared principal rows and flags, and never mount the fragment directly. Its accompanying schema rejects incomplete or extra principals. The private Teams bridge uses the stable service actor ID `teams-private-bridge`, `token_env=CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN`, `can_bind_teams_private_conversations=true`, and no takeover, Harness-write, actor-private, or credential-health grants. The Work policy continues to read the same live `TEAMS_TASK_ACTOR_SUBJECTS_FILE` projection on bind and resolve, pinned to the verified workspace, tenant, issuer, and Bot app. A stale or missing subject projection closes binding. Do not grant source access from a Work role, the actor token file, or a static profile row.

Work principal 文件必须把这些权限分开。占位符专用的 [`config/automation-work-principals.patch.template.json`](config/automation-work-principals.patch.template.json) 精确定义五个 actor writer：owner 使用 token slot 01，四名获批试点参与者使用 slot 02–05。每个 writer 都需要 `can_takeover=true` 才能修改 private Work 数据、`can_write_harness=true` 才能持久化 actor-private Harness session，并使用 `actor_private_scope=true` 作为数据可见性边界，且只绑定一个 organization-scoped Workspace。后者确保 Work 数据仍按 actor 私有；它不能替代写权限。该文件只是合并片段，不是完整 runtime principal 配置：将五个 writer 追加到现有 `principals` 数组，保留现有全局 `NAVIGATOR_SESSION_TOKEN`/`CYRENE_SESSION_TOKEN` 和 shared principal 行及 flags，不能直接挂载此片段。配套 Schema 会拒绝缺行或额外 principal。Teams private bridge 使用稳定的服务 actor ID `teams-private-bridge`、`token_env=CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN` 和 `can_bind_teams_private_conversations=true`，不能同时拥有 takeover、Harness-write、actor-private 或 credential-health 权限。Work policy 在 bind 和 resolve 时继续读取同一份实时 `TEAMS_TASK_ACTOR_SUBJECTS_FILE` 投影，并固定核对已验证的 workspace、tenant、issuer 和 Bot app。subject 投影缺失或过期时必须关闭 binding。Work role、actor token 文件或静态 profile 行均不能推导 source access。

The committed five-person template contains only replacement markers and no live IDs. Keep the existing deployment owner-only until the four pilot identities, dedicated writer tokens, and exact scopes are approved and independently read back from Directory and each source authority. The five-person map requirement is a deployment preparation boundary, not an admission grant: no non-owner target or shared visibility is enabled by the placeholder template. The actor map is only a credential lookup; it is never the Teams allowlist.

提交的五人模板只含待替换标记，不含任何真实 ID。四名试点身份、专属 writer token 和精确 scope 获批并分别从 Directory 与各 source authority 回读确认前，现有部署继续保持 owner-only。五人 map 要求是部署准备边界，不代表接纳授权：占位符模板不会启用非 owner target 或共享可见性。actor map 只用于 credential lookup，绝不是 Teams allowlist。

## Provider and workflow selection / Provider 与 workflow 选择

Graph (`cyrene.providers.microsoft-graph`) and Azure DevOps are bounded Provider processes, not Navigator vendor subprocesses. Exchange owns their existing account/profile and connector lifecycle, and Account Center retains credentials and refresh/lease handling. Graph Planner uses the same actor's Exchange connection/profile and fixed `planner_plan` binding through the existing `CYRENE_CONNECTOR_BINDINGS_FILE`; validate delegated `/me` identity against the verified caller. The workflow target ID is only a private alias lookup; the plan ID is selected in the host map. Start with the read-only `Tasks.Read` scope. Do not request `Tasks.ReadWrite`, SharePoint, Mail, Calendar, or tenant-wide task permissions for observation. The provider's write operations stay unavailable to this observation trigger.

Graph (`cyrene.providers.microsoft-graph`) 与 Azure DevOps 是受限的 Provider 进程，不是 Navigator 启动的 vendor subprocess。Exchange 管理已有 account/profile 与 connector 生命周期；Account Center 保留 credential 和 refresh/lease 处理。Graph Planner 通过现有 `CYRENE_CONNECTOR_BINDINGS_FILE` 使用同一 actor 的 Exchange connection/profile 和固定 `planner_plan` binding；必须用 delegated `/me` 验证身份并与已验证 caller 一致。Workflow target ID 仅是私有 alias lookup；plan ID 由宿主 map 选择。观察功能从只读 `Tasks.Read` scope 开始；不得为观察申请 `Tasks.ReadWrite`、SharePoint、Mail、Calendar 或 tenant-wide task 权限。该观察 trigger 不开放 Provider 写操作。

Azure DevOps observations use the fixed Exchange provider `cyrene.providers.azure-devops-observation` and operation `azure_devops.observations.poll` in the existing `CYRENE_CONNECTOR_BINDINGS_FILE`. Only the configured `pipeline_runs` and `release_deployments` target kinds are eligible. Bind them to the current actor's Exchange connection/profile and server-selected project/pipeline or release scope. Workflow fields never supply an Azure DevOps URL, organization, credential, or arbitrary project.

Azure DevOps observation 使用 `CYRENE_CONNECTOR_BINDINGS_FILE` 中固定的 Exchange Provider `cyrene.providers.azure-devops-observation` 和 operation `azure_devops.observations.poll`。只允许已配置的 `pipeline_runs` 与 `release_deployments` target kind。它们绑定到当前 actor 的 Exchange connection/profile，以及服务端选定的 project/pipeline 或 release 范围。Workflow 字段不能提供 Azure DevOps URL、organization、credential 或任意 project。

Source selection is per V3 trigger and trusted target binding. A source with no approved target, current source ACL, usable actor credential, and matching connector binding remains disabled. DSH/Navigator remains the only scheduler and durable cursor owner; do not add timers, queues, databases, or a second broker. Cadence is fixed by the validated runtime: GitHub, Azure DevOps, and Office Graph every 300 seconds UTC; VM health, Product, and Yield training every 60 seconds UTC. A source cursor is persisted per resource. An initial read is a silent baseline; an incomplete/truncated page or provider error cannot advance it. Page errors stay distinct from a valid empty page.

Source 通过 V3 trigger 和可信 target binding 逐项选择。缺少批准 target、当前 source ACL、可用 actor credential 或匹配 connector binding 时，该 source 必须保持 disabled。DSH/Navigator 仍是唯一调度器和持久 cursor owner；不要增加 timer、queue、数据库或第二个 broker。经验证的运行时 cadence 固定为：GitHub、Azure DevOps、Office Graph 每 300 秒 UTC；VM health、Product、Yield training 每 60 秒 UTC。Cursor 按 resource 持久化。首次读取只建立静默基线；未完成/被截断页面或 Provider error 不能推进 cursor。页面错误必须和有效的空页面区分。

Research lineage is reference-only. Store Product `DatasetVersion`, Yield `TrainingRun`/attempt/checkpoint, Echo evaluation, Reactor deployment, model, and commit references through the existing Work event/experiment index. Do not fetch checkpoint/model/dataset bodies or start, stop, retry, or create training jobs. Read true Yield TrainingRun/attempt states; a pod or worker marked `RUNNING` does not mean a training run is ready. No Product lifecycle state is inferred from reference metadata.

研究 lineage 仅保存引用。通过现有 Work event/experiment index 记录 Product `DatasetVersion`、Yield `TrainingRun`/attempt/checkpoint、Echo evaluation、Reactor deployment、model 与 commit 引用。不得读取 checkpoint/model/dataset 内容，也不得创建、启动、停止或重试训练任务。只读取 Yield TrainingRun/attempt 的真实状态；pod 或 worker 的 `RUNNING` 不代表 training run 已就绪。不能从引用元数据推导 Product 生命周期状态。

Workflow state remains separately disabled at both layers: retain `CYRENE_WORKFLOWS_ENABLED=false` and keep every Work-stored V3 definition `enabled=false` or absent. The Work workflow ID stays globally unique; host-generated V3 definition IDs use an actor namespace and cannot collide across private actor stores. Before any later owner-approved activation, read back the durable Work definitions and require no unexpected enabled definition. Digest delivery remains weekdays at 09:00 America/New_York, with quiet hours 22:00–08:00; immediate alerts are limited to actionable failure, auth loss, recovery, or an owner decision. Do not enable personal-chat processing as a shortcut: both the Teams package `personal_chat_enabled` value and `CYRENE_TEAMS_PERSONAL_CHAT_ENABLED` remain false with an empty actor allowlist.

Workflow 必须在两层分别保持 disabled：`CYRENE_WORKFLOWS_ENABLED=false`，Work 持久化的 V3 definition 全部 `enabled=false` 或不存在。Work workflow ID 保持全局唯一；宿主生成的 V3 definition ID 使用 actor namespace，在私有 actor store 间不得冲突。未来如经 owner 单独批准启用，须先回读持久 Work definition，并确认不存在意外启用项。摘要仅在工作日纽约时间 09:00 发送，静默时段为 22:00–08:00；即时提醒仅限可行动故障、授权失效、恢复或 owner 决策。不得把 personal-chat 处理作为捷径：Teams package 的 `personal_chat_enabled` 与 `CYRENE_TEAMS_PERSONAL_CHAT_ENABLED` 都保持 false，actor allowlist 留空。

## Provider process and VM capacity gate / Provider 进程与 VM 容量门禁

The base service stack remains unchanged. The currently reported 14-container memory budget is 3,520 MiB against a 3,584 MiB ceiling, leaving 64 MiB. Do not enable the provider overlay, shrink or resize the old services, stop the stack, or run a new container on that host. Any future provider expansion must fail closed unless fresh host evidence proves the complete service limits plus the required reserve fit the approved ceiling. `compose.automation-workflows.yaml` adds no services; it is not evidence that the separate provider overlay passes capacity.

基础服务栈保持不变。当前报告的 14 个容器内存预算为 3,520 MiB，上限为 3,584 MiB，仅剩 64 MiB。不要在该主机启用 Provider overlay、缩小或调整旧服务、停止现有栈或启动新容器。未来 Provider 扩展必须 fail closed，除非实时主机证据证明全部服务上限和所需 reserve 均符合批准上限。`compose.automation-workflows.yaml` 不增加服务，但这不能证明独立的 Provider overlay 通过容量门禁。

The approved root-owned provider overlay and its preflight check are required before building or selecting the Graph/Azure DevOps services. Use immutable image IDs from the normal source build and preserve the existing old-stack snapshot and rollback path. Azure CD remains off and user-deferred. The old GitHub `UNKNOWN_RESULT` remains on hold; do not patch SQL, replay it, or reset the existing 24-hour observation. A compose-config render, local package test, or schema validation is preparation evidence only.

构建或选择 Graph/Azure DevOps 服务前，必须先有经批准的 root-owned Provider overlay 及其 preflight 容量检查。使用正常源码构建产生的不可变 image ID，并保留旧栈快照与回滚路径。Azure CD 仍关闭且由用户延期。旧 GitHub `UNKNOWN_RESULT` 继续保持 hold；不得 patch SQL、重放或重置现有 24 小时观察。Compose 配置展开、本地 package 测试或 Schema 校验都只属于准备证据。

## Five-day measurement definitions / 五日测量定义

Start the five consecutive 24-hour windows only after an owner-approved live activation, source access read-back, capacity pass, and a recorded UTC start. Report each source independently. A local fixture, provider mock, or exporter test is `SIMULATED`; absent profiles/ACLs are `NOT_CONFIGURED`; no real VM/provider/Teams exercise is `NOT_RUN`. Do not combine these categories into a pass rate.

只有在 owner 批准的真实启用、source access 回读、容量门禁通过，并记录 UTC 开始时间之后，才开始五个连续 24 小时窗口。按 source 分别报告。本地 fixture、Provider mock 或 exporter 测试标记为 `SIMULATED`；缺少 profile/ACL 标记为 `NOT_CONFIGURED`；没有真实 VM/Provider/Teams 操作标记为 `NOT_RUN`。不得把这些类别合并成 pass rate。

| Measure | Definition and evidence | Required interpretation |
| --- | --- | --- |
| Poll coverage and status | Per source: attempted windows, complete pages, valid empty pages, partial/truncated pages, timeouts, forbidden, unavailable, and not-configured. Keep page outcomes separate; record full-page coverage as `complete pages / attempted windows`. | Report counts and percentage, with no invented success threshold. Empty is valid only when the provider returned a complete, authorized empty page. |
| Observation freshness | For each source, record `now - observedAt` at collection time; report p50, p95, max, and time beyond its configured age limit. | Preserve `stale`, `unknown`, and `NOT_CONFIGURED` as distinct states; a stale health export cannot count as healthy. |
| Cursor and event integrity | Count cursor advances after incomplete/truncated/error pages; source event exact retries; conflicting payloads for an existing dedupe key; checkpoint recovery after a controlled restart. | Incomplete/error cursor advances and immutable payload conflicts must both be zero. Exact retries may deduplicate to the original event. |
| Actor and source isolation | Count current Directory/source ACL allow/deny decisions by synthetic or privacy-safe category, including a deliberately denied out-of-scope read and a revoked grant. | Cross-actor reads and reads after revocation must be zero. A binding row or Work role is never counted as source authorization. |
| Metadata-only lineage | Count linked resource references by Product resource type; track any content/body/checkpoint bytes fetched or written by this workflow. | Artifact/body access and new training-control actions must be zero. Product run state must come from the canonical Product read, not pod state. |
| Delivery and quiet-hours behavior | Count initial-baseline notifications, unchanged-state notifications, actionable alerts, weekday digests, duplicate or out-of-scope sends, and send/read-back outcomes. | Initial baseline, unchanged sends, and duplicate/out-of-scope sends must be zero. Keep provider failure separate from Teams delivery failure. |
| Host and runtime capacity | Per day: pinned service set, latest health sample age/state, RSS/cgroup totals, available memory, provider processes present, and workflow dispatch concurrency. | Do not mutate the current VM to produce a passing result. Any provider process on the current 64 MiB headroom is a blocked capacity gate until fresh evidence proves otherwise. |

| 指标 | 定义与证据 | 必须保留的解释 |
| --- | --- | --- |
| 轮询覆盖与状态 | 按 source 记录尝试窗口、完整页、有效空页、部分/截断页、timeout、forbidden、unavailable 和 not-configured；完整页覆盖率为 `完整页 / 尝试窗口`。 | 报告数量与比例，不臆造成功阈值。只有 Provider 返回完整且已授权的空页才是有效空结果。 |
| 观察新鲜度 | 每次采集记录 `now - observedAt`；按 source 报 p50、p95、最大值和超过配置 age limit 的时长。 | `stale`、`unknown`、`NOT_CONFIGURED` 必须区分；过期 health export 不能算健康。 |
| Cursor 与事件完整性 | 统计未完成/截断/error 页面后的 cursor 推进、source event 精确重试、已有 dedupe key 的冲突 payload，以及受控重启后的 checkpoint 恢复。 | 不完整/error 页面后的 cursor 推进数和不可变 payload 冲突数都必须为零。精确重试可回到原事件并去重。 |
| Actor 与 source 隔离 | 按隐私安全类别统计实时 Directory/source ACL allow/deny，包括一次越权拒绝读和一次撤销授权。 | 跨 actor 读取与撤权后读取都必须为零。binding 行或 Work role 永远不算 source authorization。 |
| Metadata-only lineage | 按 Product resource 类型统计链接引用；追踪该 workflow 获取或写入的内容/body/checkpoint 字节。 | Artifact/body 访问和新的训练控制动作必须为零。Product run 状态必须来自 canonical Product read，不能用 pod 状态代替。 |
| 投递与静默时段 | 统计首次 baseline 通知、未变化通知、可行动提醒、工作日摘要、重复或越权发送及发送/回读结果。 | 初始 baseline、未变化发送和重复/越权发送必须为零。Provider 故障与 Teams 投递故障分开统计。 |
| 主机与运行时容量 | 每日记录固定 service 集、最近 health sample 的年龄/状态、RSS/cgroup 总量、可用内存、Provider 进程和 workflow dispatch 并发数。 | 不得为了得到通过结果而修改当前 VM。当前 64 MiB 余量下任何新增 Provider 进程都视为容量门禁阻塞，直到新证据证明可承载。 |

The measurement receipt should contain only the UTC window, source aliases, aggregate counts/percentiles, error classes, state transitions, and the evidence category. Exclude actor/resource IDs, host paths, provider bodies, tokens, message text, and artifact contents. A five-day result is observation evidence, not authorization to expand access, write Planner tasks, enable personal chat, change production, merge, or spend.

测量回执只保存 UTC 窗口、source alias、聚合数量/分位值、错误类别、状态转换和证据类别。排除 actor/resource ID、host path、Provider body、token、消息文本和 artifact 内容。五日结果只是观察证据，不构成扩大访问、写 Planner task、启用个人聊天、生产变更、合并或支出的授权。

## Activation checklist / 启用检查表

Before any later activation, the owner must verify all of the following in fresh evidence: (1) selected source package commits and immutable image references; (2) official Provider/Exchange profile and connection read-backs for the same verified actor; (3) exact target scopes and live allow/deny source ACL callbacks; (4) Work actor token map and separate private bridge principal; (5) unchanged live `TEAMS_TASK_ACTOR_SUBJECTS_FILE` and owner-only actor-private Work data; (6) private map/secret file ownership, permissions, and hash inventory without printing values; (7) personal Bot install disabled unless separately approved; (8) all Work V3 definitions absent or disabled while the engine is off; (9) provider overlay capacity preflight passes against current host facts; (10) measurement windows and the authorized report destination are recorded. Any missing item remains `NOT_CONFIGURED` or `NOT_RUN` and blocks activation.

未来任何启用前，owner 必须用最新证据核对以下全部项目：(1) 所选 source package commit 和不可变 image 引用；(2) 同一已验证 actor 的官方 Provider/Exchange profile 与 connection 回读；(3) 精确 target scope 和实时 allow/deny source ACL callback；(4) Work actor token map 与独立 private bridge principal；(5) 未变更的实时 `TEAMS_TASK_ACTOR_SUBJECTS_FILE` 及 owner-only actor-private Work 数据；(6) 私有 map/secret 文件 owner、权限和摘要清单，且不打印值；(7) 除非另行批准，Personal Bot install 保持 disabled；(8) engine 关闭期间所有 Work V3 definition 均不存在或 disabled；(9) Provider overlay 容量 preflight 针对当前主机事实通过；(10) 记录测量起止时间与已授权的回执位置。任何缺项都保持 `NOT_CONFIGURED` 或 `NOT_RUN` 并阻止启用。

No live activation, provider consent, real Teams personal-chat test, provider read, or VM capacity operation was performed while preparing this document. See [the host health export contract](AUTOMATION_OBSERVATION_EXPORT.md) for the sanitized VM status boundary and [the Teams deployment runbook](../../../docs/TEAMS_CLOUD_DEPLOYMENT.md) for existing host preparation and rollback rules.

编写本文时未执行真实启用、Provider consent、Teams personal-chat 测试、Provider 读取或 VM 容量操作。脱敏 VM 状态边界见[主机健康导出合约](AUTOMATION_OBSERVATION_EXPORT.md)，既有主机准备和回滚规则见[Teams 云部署手册](../../../docs/TEAMS_CLOUD_DEPLOYMENT.md)。
