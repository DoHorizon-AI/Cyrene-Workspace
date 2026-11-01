# Business Agent repair and migration notes

This note records the behavior that a later legacy-project or Aether integration
must preserve. Repository-local PRs remain the authority for each implementation;
this document does not enable a workflow, grant permission, or deploy an image.

## Observed failures and ownership

| Failure | Owning boundary | Required behavior |
|---|---|---|
| Task completes but a private result is not received in Teams | Navigator Work outbox and the verified Teams personal-installation bridge | Keep the result private and durable; distinguish task completion from message delivery. Repair installation or bridge readiness through the supported path. |
| PR listing works for a small repository but fails for a larger one | Plugins GitHub provider; Exchange validates connector operation arguments | Keep the response-size bound, request bounded pages, and expose explicit continuation and completeness facts. |
| Agent declares a commit absent from branch history using only PR timestamps or base SHA | Navigator's model adapter and evidence-producing provider tools | Use an explicit ancestry/compare result, or report that the relationship is unknown. |
| Agent repeats an identical failed request after `retryable=false` | Navigator model/tool-result handling | Preserve the structured failure and choose a meaningful different query or stop with the actual limitation. |
| Connection UUID is supplied as GitHub repository ID | Tool input-schema projection and provider resource discovery | Preserve schema constraints and distinguish Cyrene binding identity from vendor resource identity. |
| Empty legacy commit statuses are treated as unfinished required checks | GitHub check/status projection and model interpretation | Report legacy statuses, check runs and completeness separately; required-context claims need branch-protection evidence. |

The PR-list failure was reproduced with an upstream page of 100 items exceeding
the provider's 1 MB response limit. Authentication worked. Increasing that limit
or converting the error into an empty list would hide the failure. A bounded
page and a validated continuation token address the cause while retaining the
resource bound. Old callers that supply only `state` must remain valid; absence
of later pages must never be reported as an exhaustive repository result.

The private-notification hold is a deliberate confidentiality boundary. A channel
request can use a user-bound credential and produce data that the entire channel
cannot read. Missing personal installation must not cause a fallback to the
channel, a different actor, or an arbitrary conversation ID. Microsoft documents
the [personal installation prerequisite](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages)
and the separate [Graph installation permissions](https://learn.microsoft.com/en-us/graph/api/userteamwork-post-installedapps?view=graph-rest-1.0).

## Resolving later source conflicts

1. Keep provider transport, pagination and supplier resource identifiers in
   Plugins. Keep Exchange's signed target/argument validation consistent with the
   provider schema. Do not copy vendor implementation into Navigator or Client.
2. When merging a connector catalog or SDK from the legacy project, retain the
   pagination cursor constraints, response completeness facts and old-call
   compatibility. Regenerate the source manifest from the resulting tree rather
   than choosing either side's manifest wholesale.
3. When merging a conversation/model adapter, preserve the existing CodeBuddy
   ACP session and actor isolation. Add evidence guidance to the existing
   system/developer prompt path; do not replace it with a second execution loop
   or a repository-specific expected answer.
4. Retain the structured tool error, including its retryability. Prompt guidance
   is a model behavior aid, not a proof that all future answers are correct.
   Compare/ancestry must come from actual data, including when an earlier
   conclusion is challenged.
5. Preserve Work's private outbox, verified installation provenance, leases,
   deduplication and uncertain-send handling. A locally populated chat ID is not
   proof of a real installation. Do not edit SQLite to release a held result.
6. Merge narrowly scoped hunks and rerun the affected tests. A clean Git merge
   does not prove agreement on the authority or privacy boundaries above.

## Production update and acceptance

Source merge and VM activation are separate gates. Build immutable candidates
from recorded commits, review the exact diff against the active images, preserve
rollback images and persistent state, and use the existing drain, health and
runtime-selector mechanisms. Retain the selected model, credential stores and
unselected services. If a replacement or send has an unknown result, reconcile
the original operation before any replay.

Acceptance must record these independent facts:

- Fresh branch SHA and PR metadata from the provider, including later pages.
- CodeBuddy tool request, actual provider result, resumed turn and final answer.
- Correct handling of unsupported ancestry claims and non-retryable failures.
- Real Teams actor, first mention and an unmentioned continuation in one thread.
- Durable notification state and a real vendor message ID, independently read
  back; `task.status=completed` alone is insufficient.
- Reuse of the existing private result after installation recovery, without
  re-executing the original business task or sending it to a public channel.

The VM has no implicit knowledge of desktop Git cleanup. A truthful “unknown” is
expected until a separately authorized, verifiable receipt source exists.

## Existing Graph-phase GitHub updates

The selected Graph-bootstrap runtime already includes the GitHub service. Its
single-image update path must preserve the exact fifteen-service topology and
3584 MiB / 1.95 CPU budget, join the one live Graph binding/catalog while keeping
Graph out of the prepared candidate binding/catalog, and verify all ten frozen
GitHub repository connections. Keep the GitHub monitor/readiness checkpoints in
the journal and before/after apply. Adding GitHub to a phase allowlist alone is
insufficient. Ordinary provider-bootstrap still rejects this target; issued
activation retains its existing path. This image-only continuation does not
authorize a catalog, profile, credential or repository-scope change.

For personal app publication, read and validate the parent Graph `teamsApp`
before linking a definition: `teamsApp.id` must match the catalog ID used in
the endpoint, and `teamsApp.externalId` must match the expected manifest ID.
In this tenant's observed response, `teamsAppDefinition.teamsAppId` returned
the parent catalog ID. Microsoft's resource documentation describes that
property as the manifest ID, so the observed tenant response differs from the
documented contract. Do not unconditionally interpret `teamsAppDefinition.teamsAppId`
as the manifest ID. After validating the parent record, bind the definition
foreign key to the verified parent catalog ID and check the returned definition
link against that relationship. Missing, null, or mismatched identity fields
must produce a clear contract error rather than an unhandled `.lower()` failure.
Keep strict checks for the bot, `clientAppId`, RSC permissions, and version;
normalize `permissionType` enum casing to Graph's canonical lowercase spelling
(for example, `application`) before comparison without relaxing its required
semantics. See Microsoft's
[definition contract](https://learn.microsoft.com/en-us/graph/api/resources/teamsappdefinition?view=graph-rest-1.0).

## Private registry archives and bounded VM retention

Use a private OCI registry for historical runtime images. Retain the current
selected images and the reviewed recent rollback set on the VM; archive older
images before considering local eviction. Sessions, credentials and databases
remain in their existing persistent stores and are not image backups.

For each archived image, record the original local reference, Docker image ID,
platform, source revision, immutable registry manifest digest and config digest.
These identifiers are distinct: a containerd image store can report the manifest
digest as its Docker image ID. A real authenticated pull must preserve the
expected local identity; do not assume that image ID always equals config hash.
Verify private visibility and denied anonymous access before uploading runtime
data. Establish the archive package without inheriting a public repository's
access permissions; keep upload credentials off the VM. A temporary,
repository-scoped pull token can verify recovery without giving the VM package
write permission. Remove that temporary credential after the check.

Local retirement requires fresh container references, current selector and
rollback-lock checks, verified remote recovery, and an exact image-ID plan.
Remove only unused reviewed images; do not use broad image/volume pruning or
delete an active or unarchived rollback image. Restore the original local tag
and verify the expected image ID before using an older saved deployment plan.
A registry archive receipt does not turn a `LOCAL_ONLY` candidate into an
approved full-stack release or replace the existing deployment/provenance gates.

Operator-authenticated recovery and unattended registry authorization are
separate capabilities. Do not claim that server-side credential renewal or
automatic CD is configured merely because a manual push and pull succeeded.

---
<!-- Chinese Translation / 中文翻译 -->

## 修复原因与后续老项目合并要求

本文记录后续老项目或 Aether 合入时应保留的语义；各仓库 PR 才是具体实现记录。
本文不会启用工作流、增授权限或部署镜像。

大仓库 PR 列表失败已复现为上游每页 100 项超过 Provider 的 1 MB 响应上限，
而非认证失败。修复应保留上限、使用有界分页、校验后续游标，并明确是否完整。
旧的只传 `state` 的调用应继续可用，不能把第一页或错误伪装成完整空列表。

Teams 任务完成与消息送达是两个事实。使用个人凭据得到的结果可能不允许整个
频道查看；因此缺少个人安装时，必须保留私有 outbox 并通过正式安装和桥接流程
恢复，不能回退公共频道、改接收人、手写 chat ID 或直接改 SQLite 放行。

后续解决冲突时重点保留：

1. 供应商传输与分页属于 Plugins；Exchange 的签名目标和参数校验必须与其一致。
   不把实现搬入 Navigator 或 Client；合并后重新生成最终 source manifest。
2. GitHub repository ID 与 Cyrene connection UUID 含义不同。模型工具 schema
   不能丢失关键格式约束，也不能以高权限默认值替换无效输入。
3. Navigator 沿用 CodeBuddy ACP、现有 actor/session 隔离与唯一执行链。
   证据规则应加在正式 system/developer 消息通道，不覆盖原策略，不硬编码答案。
4. PR 时间和 base SHA 不能证明某提交不在分支历史中。没有实际 ancestry/compare
   证据时应明确未知；`retryable=false` 不应触发相同请求的机械重试。
5. legacy commit statuses、check runs、分页完整性和 required contexts 分开报告。
   statuses 为空不能推出必需检查还未结束。
6. 保留私有投递的安装来源验证、租约、去重和 uncertain 状态；提示词改进不等于
   所有未来回答均正确，仍需真实验收。

源码 PR 合并后，VM 切换另行验收：固定候选提交与镜像摘要、核对差异、保留回退
镜像和持久数据，使用现有 drain / health / selector 更新机制，只替换必要组件。
执行或发送结果不确定时先回读，不能自动重复创建操作或发送消息。

验收分别记录：真实 Provider 查询、后续分页、工具结果续轮、无提及续问、真实
owner 身份、通知持久状态及供应商消息 ID。个人安装恢复后，应由既有 outbox
投递原结果，不重跑业务任务。台式机清理记录没有正式数据来源时，Agent 回答
“不知道”符合当前边界。

### Graph 阶段更新和 Teams 应用身份

Graph-bootstrap 的现有十五服务已经包含 GitHub。本次只增加其单镜像延续路径，
仍须验证 3584 MiB / 1.95 CPU、唯一 live Graph binding/catalog 与候选中无 Graph
条目的关系、十个冻结的 GitHub 仓库连接，以及 journal/apply 前后的监视静默和
readiness。不能只放宽 allowlist；普通 provider-bootstrap 仍拒绝，原 issued 路径
保留。镜像更新不同时扩大 catalog、profile、凭据或仓库范围。

发布个人应用时，先读取并校验父 Graph `teamsApp`：`id` 必须匹配接口路径使用的
catalog ID，`externalId` 必须匹配预期的 manifest ID。本租户实际读取到的
`teamsAppDefinition.teamsAppId` 是父 catalog ID；微软资源文档则将该字段描述为
manifest ID，因此实际响应与文档契约存在差异。不要仅凭字段名就无条件把
`teamsAppDefinition.teamsAppId` 当作 manifest ID。校验父记录后，definition 外键应绑定
到已验证的父 catalog ID，并检查返回的 definition link 与父记录关系一致。任何必需
身份字段缺失、为 null 或不匹配，都应返回明确的合同错误。继续严格校验 bot、
`clientAppId`、RSC 权限和 version；比较前将 `permissionType` 归一为 Graph 规范的小写枚举
（例如 `application`），但不放宽权限语义。个人发布流程的授权、真实 Graph 结果和撤销回读
仍单独验收。

### 镜像远端归档与 VM 保留范围

历史镜像放到私有 OCI 仓库，VM 保留当前选择器使用的镜像和已审核的近期回退集。
会话、凭据与数据库仍在原持久存储中，不能把镜像归档当作这些数据的备份。

逐镜像记录原 tag、Docker image ID、架构、源码版本、registry manifest digest 和
config digest。containerd 可能以 manifest digest 作为 image ID；必须真实拉取
核验，不能固定假设 image ID 就是 config hash。先验证私有访问与匿名拒绝，
避免归档仓库继承公开源码仓库的读取权限；上传凭据不放到 VM，验证拉取使用
临时、限定仓库的只读 token 并在结束后移除。

清理前重新核对容器引用、当前选择器和回退锁、远端恢复证据及精确 image ID。
只删除已审核且未使用的旧副本，不运行全局 image/volume prune。恢复历史计划前
先拉取固定 digest、恢复原本地 tag 并核对 ID；远端归档不绕过已有发布与部署门禁。
人工授权下推送/拉取通过，不等于已经配置无人值守续期或自动 CD。
