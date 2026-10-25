# Teams cloud deployment / Teams 云端部署

This runbook describes the ARM64 deployment bundle in [`tooling/deploy/teams-cloud/`](../tooling/deploy/teams-cloud/README.md). It keeps Navigator as the sole task executor, Exchange as the connector and reply boundary, Directory as the maintainer authority, and the Teams ingress sidecar as a narrow public callback adapter.

本手册说明 [`tooling/deploy/teams-cloud/`](../tooling/deploy/teams-cloud/README.md) 中的 ARM64 部署包。Navigator 是唯一任务执行器；Exchange 负责连接器和回复边界；Directory 是维护者授权依据；Teams ingress sidecar 只承接受限的公网回调。

## Evidence boundary / 证据边界

The bundle provides build, transfer, configuration, read-only preflight, and bounded resource-cleanup code. It does not prove that the current VM has enough disk or memory for the built images, that Teams DNS/callback has been switched, or that GitHub, Hugging Face, Azure, GCP, or RunPod operations passed. Treat those checks as `NOT_RUN` until a fresh receipt records the exact source/image IDs, host facts, and provider read-backs. The previous Docker stack must stay available until staging, Teams thread continuation, connector checks, and rollback have passed.

本部署包提供构建、镜像传输、配置、只读预检，以及受预算限制的资源回收代码。它不能证明当前云机有足够磁盘或内存承载构建镜像，不能证明 Teams DNS/回调已切换，也不能证明 GitHub、Hugging Face、Azure、GCP 或 RunPod 操作验收通过。除非新证据记录了精确源码和镜像 ID、主机容量及供应商回读，否则这些验收均为 `NOT_RUN`。在 staging、Teams 原帖续问、连接器检查和回滚通过前，旧 Docker 栈必须保持可恢复。

The production gate is native Linux ARM64, at least 2 CPUs and the nominal 4 GiB RAM class (at least 3584 MiB guest-visible `MemTotal` after firmware/kernel reservations), at least 2 GiB root-free reserve after transfer, and an online Tailscale daemon. The proactive profile adds the isolated computer runner and its mTLS proxy: Stage 1 caps persistent services at 2,880 MiB and 1.80 CPUs; Stage 2 and Stage 3 cap them at 3,264 MiB and 1.90 CPUs. One-shot Directory/bootstrap commands are excluded and run separately. Preflight reads current `MemAvailable` and requires at least 1,024 MiB before staging or cutover. If the full cgroup budget plus reserve does not fit yet, staging is allowed only to collect real per-service RSS; cutover then requires healthy staged services, RSS read-back, and the same live memory reserve. The transfer helper measures each image's conservative materialized disk footprint, including rootfs and compressed layers, and requires `1.10 × image footprints + runtime reserve` free before loading. Compressed archive size and a historical free-space report are not substitutes for that measured gate. The added budget is a configured ceiling, not a claim that the current VM can support it; read the live host before staging and do not resize automatically.

生产门槛为原生 Linux ARM64、至少 2 CPU 和 4 GiB RAM 规格（扣除固件和内核保留内存后，guest 可见 `MemTotal` 至少 3584 MiB）、镜像传输后根盘仍至少空闲 2 GiB，并且现有 Tailscale daemon 在线。主动助手 profile 增加独立电脑执行容器和 mTLS 代理：Stage 1 常驻服务 cgroup 总上限为 2,880 MiB 和 1.80 CPU；Stage 2/3 为 3,264 MiB 和 1.90 CPU。Directory/bootstrap 一次性任务不计入常驻总量，须单独运行。预检读取实时 `MemAvailable`，staging 和切换前都至少保留 1,024 MiB。如果完整 cgroup 上限加预留空间暂时放不下，仍可进行 staging 来实测每个服务 RSS；切换预检还会要求服务健康、RSS 回读完整且实时内存预留达标。镜像传输脚本会实测包含 rootfs 和压缩层的保守落盘占用，并要求传输前可用空间达到“镜像占用总和 × 1.10 + 运行时预留”。压缩包大小和历史空闲空间读数均不能替代该测量门槛。这些是配置上限，不代表已核实当前 VM 能承载；staging 前必须读取实时主机容量，不自动扩容。

## Private inputs and identities / 私有配置与身份

Copy `config/runtime.env.template`, `config/secret-files.template.json`, `config/navigator-principal-config.template.json`, `config/teams-actor-subjects.template.json`, `config/teams-connector.template.json`, `config/connector-bindings.schema.json`, `config/connector-bindings.bootstrap.schema.json`, `config/connector-bindings.empty.template.json`, `config/connector-tool-catalog.schema.json`, `config/connector-tool-catalog.bootstrap.schema.json`, `config/connector-tool-catalog.empty.template.json`, `config/private-connector-bootstrap.template.json`, `config/cloud-api-evidence.schema.json`, both `config/cloud-api-evidence.*.template.json` files, and `config/build-plan.template.json` to a task directory under the umbrella `Cyrene/.artifacts/teams-cloud-<task>/`. The deployment config and receipt directories must remain outside every source worktree. Replace each template only with verified non-secret values; keep credential material in separate owner-only files under the matching private `secrets/` directory.

将 `config/runtime.env.template`、`config/secret-files.template.json`、`config/navigator-principal-config.template.json`、`config/teams-actor-subjects.template.json`、`config/teams-connector.template.json`、`config/connector-bindings.schema.json`、`config/connector-bindings.bootstrap.schema.json`、`config/connector-bindings.empty.template.json`、`config/connector-tool-catalog.schema.json`、`config/connector-tool-catalog.bootstrap.schema.json`、`config/connector-tool-catalog.empty.template.json`、`config/private-connector-bootstrap.template.json`、`config/cloud-api-evidence.schema.json`、两个 `config/cloud-api-evidence.*.template.json` 文件和 `config/build-plan.template.json` 复制到 umbrella 的 `Cyrene/.artifacts/teams-cloud-<task>/` 任务目录。部署配置和回执目录必须位于所有源码工作树之外。模板只填入已核实的非敏感值；凭据分别放在对应私有 `secrets/` 目录中，并设置为仅所有者可读。

The three Directory URLs have separate PostgreSQL logins and mounts:

Directory 的三种 PostgreSQL URL 使用不同登录名和挂载位置：

| URL file | Allowed use | Database role |
| --- | --- | --- |
| `directory-admin-database-url` | One-shot schema migration and role bootstrap only | `cyrene_directory_admin` |
| `directory-operator-database-url` | One-shot maintainer add/revoke utility and role bootstrap | Login member of `cyrene_workspace_directory_operator` |
| `directory-runtime-reader-database-url` | Connector Authorization runtime and role bootstrap | Login member of `cyrene_workspace_directory_reader` |

All three URLs target the private `postgres` service and must use `sslmode=verify-full` with `/run/secrets/postgres-root-ca`. Migration creates the two `NOLOGIN` group roles. Role bootstrap creates/verifies the dedicated operator and runtime login roles, grants only their matching groups, and verifies reader writes are denied. No long-running service receives the admin or operator URL. Postgres root password, migration URL, operator URL, and runtime reader URL must not share one file.

这三种 URL 均连接内网 `postgres` 服务，并通过 `/run/secrets/postgres-root-ca` 使用 `sslmode=verify-full`。迁移会创建两个 `NOLOGIN` 组角色。角色引导任务只创建或核实专用 operator/runtime 登录角色、授予各自匹配的组，并验证 reader 无法写入。常驻服务不会收到 admin 或 operator URL。Postgres 初始化密码、迁移 URL、operator URL 和 runtime reader URL 不得共用文件。

The first Teams actor is represented in the private `teams-actor-subjects.json` file as `{ "schemaVersion": 1, "workspaceId": "…", "issuer": "https://login.microsoftonline.com/<tenant>/v2.0", "subjects": ["<owner AAD object ID>"] }`. Navigator reads this projection for each admission and reply. The Teams ingress validates the Bot Framework identity, tenant, and the one approved team, then forwards the authenticated actor in the signed event. The actor file is not a connector grant: every provider tool call still obtains a fresh Authority grant through Exchange.

首位 Teams 身份写入私有 `teams-actor-subjects.json`，格式为 `{ "schemaVersion": 1, "workspaceId": "…", "issuer": "https://login.microsoftonline.com/<tenant>/v2.0", "subjects": ["<owner AAD object ID>"] }`。Navigator 在每次接纳和回复时读取该投影。Teams ingress 验证 Bot Framework 身份、租户和唯一获准团队，再把已认证身份放入签名事件。该文件不等同于连接器授权；每次 Provider 工具调用仍须经 Exchange 向 Authority 获取新 grant。

`maintainer_subjects.py` is a host-invoked, one-shot wrapper around the real `cy-workspace-directory-admin maintainer add|revoke` command. It accepts only a target AAD object ID and an audit reason, uses the operator URL, and updates the actor file only after Directory succeeds. The file replacement is mode `0600`, same-directory, fsynced, and atomic so the read-only Navigator bind mount observes the replacement. A Directory timeout is `UNKNOWN` and must be reconciled before retry. A database success followed by projection-write failure is reported as a partial failure and must be repaired before reporting completion. The wrapper refuses to revoke its configured owner operator.

`maintainer_subjects.py` 是宿主机调用的一次性包装器，调用真实的 `cy-workspace-directory-admin maintainer add|revoke` 命令。它只接收目标 AAD Object ID 和审计原因，使用 operator URL，并且只在 Directory 成功后更新 actor 文件。文件通过同目录临时文件、`0600` 权限、`fsync` 和原子替换写入，因此 Navigator 的只读目录挂载可看到新内容。Directory 超时视为 `UNKNOWN`，核对状态前不得重试。数据库成功但投影写入失败会明确报告部分失败，修复前不能报告完成。包装器禁止撤销配置的 owner operator。

The private `navigator-principal-config.json` configures this owner as a trusted durable Work writer (`can_takeover: true`) so Navigator can persist Tasks and admit Teams events. Its `product_directory` and `product_service_credentials` stay empty; the Work writer credential does not grant connector rights. Directory membership and the fresh per-call Authority grant remain the source of connector authorization.

私有 `navigator-principal-config.json` 将该 owner 配置为可信的持久 Work writer（`can_takeover: true`），使 Navigator 能持久化任务并接纳 Teams 事件。其 `product_directory` 与 `product_service_credentials` 仍保持为空；Work writer 凭据不授予连接器权限。连接器授权仍由 Directory 成员关系和每次调用时新签发的 Authority grant 决定。

## Build and lock / 构建与锁定

Use a clean, committed Workspace source tree and fill every `sources.*.commit` in the private build plan with the exact reviewed remote SHA. The DSH commit must equal the `DSH_COMMIT` build argument. The builder fetches and archives those commits into a temporary context; it never copies from the dirty umbrella checkout, sibling worktrees, or a personal `~/.codebuddy` directory. It resolves each base and external image tag to a registry index digest and its unique ARM64 child manifest, then records each produced image ID, source SHA, `linux/arm64` platform, and measured size.

使用干净且已提交的 Workspace 源码树，并在私有 build plan 中为每个 `sources.*.commit` 填入精确且已审查的远端 SHA。DSH 源 SHA 必须与 `DSH_COMMIT` 构建参数相同。构建器将这些提交 fetch/archive 到临时构建上下文；不会从脏 umbrella 检出、同级工作树或个人 `~/.codebuddy` 目录复制文件。构建时把每个基础/外部镜像标签解析为 registry index digest 和唯一 ARM64 子 manifest，并记录产出镜像 ID、源码 SHA、`linux/arm64` 平台及实测大小。

Run the builder on an ARM64-capable Buildx builder. On an x86 host, `--allow-emulated` is an explicit opt-in for QEMU and is not evidence of a native production build. `LOCAL_ONLY` locks intentionally contain a real local image ID and `registryDigest: null`; never fill a remote digest by hand. A published lock may transition to `PUBLISHED` only after the registry returns and the operator verifies each exact app-image digest. Caddy and Postgres are separately locked by the same resolved ARM64 image-index metadata.

在支持 ARM64 的 Buildx builder 上运行构建器。在 x86 主机上，`--allow-emulated` 是显式启用 QEMU 的选项，不能证明生产原生构建。`LOCAL_ONLY` 锁包含真实本地镜像 ID 和 `registryDigest: null`；不得手工填写远端 digest。只有 registry 返回并且操作员逐一核实每个应用镜像的精确 digest 后，才可将锁更新为 `PUBLISHED`。Caddy 和 Postgres 也会记录已解析的 ARM64 镜像元数据。

```sh
python tooling/deploy/teams-cloud/build_arm64.py build \
  --plan /path/to/Cyrene/.artifacts/teams-cloud-<task>/config/build-plan.json \
  --output /path/to/Cyrene/.artifacts/teams-cloud-<task>/build

python tooling/deploy/teams-cloud/build_arm64.py verify-local \
  --lock /path/to/Cyrene/.artifacts/teams-cloud-<task>/build/image-lock.json
```

## Proactive assistant profile / 主动助手配置

The opt-in v1 stack adds `compose.proactive-assistant-v1.yaml` and `compose.computer-runtime.yaml` to the base file. The former sets Antigravity `gemini-3.8-flash-medium` as the regular model, with the configured CodeBuddy fallback only when the provider profile has `CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP=true`. There are no per-day, per-turn, run-count, or repair-count gates. The executor keeps two bounded task slots (one reserved for foreground chat and one for background work); individual provider/tool operations retain their own timeouts and upstream limits, while a durable Work Task has no total-run timeout by default. This preserves ordinary Teams chat while a scheduled responsibility job runs.

The Antigravity profile template is model-only: no shell tools, MCP servers, subagents, browser automation, or added plugins. Keep the CLI login/cache home at `CYRENE_MODEL_HOME` under the private task artifact directory, owned by UID 10001 and mode 0700. Keep Navigator task/session state in its persistent `navigator-sessions` volume. Keep the separate CodeBuddy provider profile under the private `secrets/codebuddy` path. Neither profile nor any provider token is copied into an image. Back up these profile directories and the business-state volumes separately, encrypt backups at rest, and retain the owner-only permissions when restoring.

The responsibility target file is host-managed configuration, not model output. Copy `config/responsibility-targets.v1.template.json` to the private `config/responsibility-targets.v1.json`; populate the ten exact public repository IDs from canonical `repositories.yaml` plus current GitHub App installation scope, then replace the unbound marker only after Exchange re-issues the GitHub profile binding for the complete approved set. This delivery bundle includes an owner-only generated list at `.artifacts/proactive-assistant-v1-20261024/config/responsibility-targets.v1.json`; its binding marker is intentionally unresolved because the existing production connector profile covers only a smaller scope. Do not enable the workflow or change the active runtime until the rebind is completed and read back. The target grant allows draft work only on `develop`, using task branches under `cyrene/assistant/`; failed source CI can be rerun only on those task branches and only for the explicit per-repository `sourceWorkflowIds` allowlist. The Workspace rerun grant is omitted because its CI workflow also creates immutable release artifacts. `pathPrefixes: ["**"]` means any repository-relative source path; the GitHub provider still independently denies hidden, control, deployment, and production paths. No issue update grant is provisioned.

Use `config/proactive-assistant-workflow.v2.template.json` as the disabled workflow definition template. Populate its target IDs and current binding IDs from the host target file, and fill `authorization.threadBindingId` plus `authorization.ownerTaskId` from a freshly verified owner Teams thread. Keep `enabled:false` until both IDs and connector grants are confirmed. Register through Navigator Workflows v2 (`PUT /api/v1/workspaces/{workspaceId}/work/workflows/{id}?definitionVersion=2`). The durable workflow polls GitHub every five minutes UTC; its first poll records a baseline without notification. It reviews all ten repositories at 09:00 and 17:00 America/New_York, sends at most one daily digest at 09:00, stays quiet on unchanged results from 22:00 to 08:00, and reports blocked goals, owner decisions, or service/auth outages immediately to the responsibility thread. It cannot merge, release, change production, or create new spend.

The computer runner exposes only the fixed command/file/directory API through an mTLS Caddy proxy on private Docker networks. Its Exchange binding is optional host configuration: merge `config/computer-runner.integration-fragment.v1.template.json` into the generated `CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON` root, preserving existing connectors. Resolve the stable connection UUID, Directory organization/workspace IDs, tenant issuer, and certificate files from the live host before enabling it; the fragment's placeholders must never pass preflight. Compose provides Exchange with the existing plugin CA/client certificate/key paths and keeps the runner free of those credentials. The runner runs as UID/GID 10001 with a read-only root, no Linux capabilities, no credentials, no model profile, no Docker socket, and no external network. Its only writable mounts are a dedicated workspace and artifact volume. Limits are 30-second default and 10-minute maximum command timeouts, 64 KiB per stdout/stderr stream, 64 KiB file reads, 1 MiB file writes, 2,048 directory entries, depth 32, and 16 MiB artifact size. This is a low-privilege operational boundary, not a hostile-code sandbox: use CI for heavy builds and high-risk commands. V1 provides one shared workspace per runner and has no per-task filesystem isolation or call serialization. The scheduled responsibility workflow uses typed GitHub tools only; do not add background computer actions or permit concurrent computer tasks until Navigator/Exchange enforces single-writer coordination.

Before enabling background work, back up the current service configuration, image lock, Navigator persistence volume, DSH state, and provider auth directories into separate owner-only snapshots; verify the backup manifest and restore readability. Identify the old and new Navigator/DSH persistence schema versions, then use the supported export/import or migration for those exact versions in a cloned volume. Reconcile task, message, thread-binding, and event counts and exercise continuation from an existing Teams thread before cutover. Do not byte-copy a live database or assume an incompatible old volume can be mounted by the new image. If no supported migration exists, keep the old stack authoritative and defer cutover until a migration is implemented. Build and stage the new stack while the old stack remains recoverable. Do not allow both schedulers to run simultaneously: stop the old workflow scheduler only after the new stack passes health and chat checks, then enable the new workflow. On failure, disable the new workflow, stop its services, restore the previous config/state snapshot, and bring the old stack up; verify only one bot callback sender and one scheduler are active. Keep rollback credentials separate from business-state snapshots. The initial cutover, Teams callback, real repository reads, restart recovery, and 24-hour observation remain `NOT_RUN` until performed against the actual VM. The total write-acceptance budget is at most USD 5; each temporary test resource may run no longer than 15 minutes, and every created resource must be deleted and absence re-read.

当前 VM 的身份、容量、磁盘、Docker 状态、镜像构建/尺寸、Teams 回调和 24 小时稳定性均未在本轮查询，仍为 `NOT_RUN`。本轮只完成本地配置与打包准备；不得将文档中的限制或模板当成云端验收证据。

### 主动助手配置说明 / 中文说明

主动助手 v1 需要显式叠加 `compose.proactive-assistant-v1.yaml` 和 `compose.computer-runtime.yaml`。默认模型是 Antigravity `gemini-3.8-flash-medium`；CodeBuddy 只作为已配置的无额外付费回退。聊天、轮次、每日运行和修复次数都没有上限；并发槽位、单次供应商/工具超时和上游限制仍然生效。Navigator 持久化 Work Task 不设置默认总时限，因此用户聊天可与一个后台任务并行。

Antigravity CLI 使用私有 task artifact 下 UID 10001 所有、权限为 0700 的 `CYRENE_MODEL_HOME`；CodeBuddy profile 保存在独立的私有 `secrets/codebuddy`；Navigator 会话和业务状态在持久化卷中。不要把任一供应商登录目录或 token 打进镜像。备份时分开保存并加密模型授权目录、Navigator 业务状态和服务配置，恢复后继续保持 owner-only 权限。

仓库目标配置只由宿主机维护。所有十个仓库的 ID 来自 Workspace 规范拓扑和当前 GitHub App 安装范围；现有生产 Exchange binding 范围较小，必须先重新绑定并回读后才能替换 `UNBOUND_PENDING_EXCHANGE_GITHUB_REBIND`。在绑定和 Teams owner 帖子/Task 标识就绪前，工作流保持 `enabled:false`。工作流每五分钟读取开放事项，首次只建立静默基线；每天纽约时间 09:00 和 17:00 汇总审查，每天最多推送一份 09:00 摘要，22:00 至 08:00 对未变化状态保持安静。阻塞、需要 owner 决策或服务/授权中断会立即发到责任帖子。仅允许 `develop` 到 `cyrene/assistant/` 下生成草稿 PR；重跑 CI 只允许明确列出的源 CI workflow 和同一命名空间内的分支。Workspace 的候选 workflow 会产出不可变发行物，因此不授予自动重跑；Issue 状态更新、合并、发布、生产变更和新支出都不在默认授权内。

电脑执行服务位于单独的低权限容器中，只经内网 mTLS 代理提供固定命令、文件和目录 API。Exchange 绑定属于可选的宿主配置：将 `config/computer-runner.integration-fragment.v1.template.json` 合并进已生成的 `CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON` 根对象，并保留现有连接器配置。启用前必须从实际主机核实连接 UUID、Directory 组织/工作区 ID、租户 issuer 和证书文件；片段中的占位值不能通过预检。Compose 通过现有只读挂载把插件 CA、Exchange 客户端证书和密钥路径交给 Exchange；运行器本身没有这些凭据。运行器 UID/GID 为 10001，根目录只读，无 Linux capability、无模型/供应商凭据、无 Docker socket、无外网。它仅能写专用 workspace 与 artifact 卷。单条命令默认 30 秒、最多 10 分钟；stdout/stderr 各 64 KiB、单次文件读取 64 KiB、写入 1 MiB、目录最多 2,048 项/深度 32，artifact 最大 16 MiB。该版本所有调用共享同一个工作目录，尚无按 Task 隔离或并发调用串行机制。主动责任工作流只用 GitHub typed tools；新增会调用电脑执行器的后台流程前，必须先由 Navigator/Exchange 强制单写者协调。高风险或大构建仍交给 CI。

切换前应分别备份当前配置、镜像锁、Navigator 持久卷、DSH 状态和供应商认证缓存，并验证备份清单和可读性。核对新旧 Navigator/DSH 的持久化 schema 版本，在克隆卷中使用对应版本支持的导出/导入或迁移；比对 Task、消息、帖子绑定和事件数量，并从既有 Teams 帖子实测续聊。不要在数据库仍在线写入时直接复制，也不要假定新镜像能挂载不兼容的旧卷。若没有受支持的迁移方式，继续以旧栈为准，推迟切换直到迁移实现。先 staging 并保留旧栈的恢复路径；不得同时运行两套定时调度器。只有新栈健康并通过 Teams 双向聊天后，才停止旧调度器并启用新工作流。失败时先停用新工作流和新服务，再恢复旧配置与状态，确认只有一个 Bot 回调发送方和一套调度器。每轮真实写入验收总预算不超过 5 美元；每个临时资源最多运行 15 分钟，并且必须删除后再读回确认不存在。

## Directory bootstrap and staging / Directory 引导与 staging

Render Compose with the private runtime env, generated `images.env`, and the exact Compose overlays selected for that phase. First start Postgres, then run the profile-gated migration and role-bootstrap one-shots in order. They are excluded from ordinary `up`; they are not dependencies of every service restart.

使用私有 runtime env、生成的 `images.env` 和该阶段所需的精确 Compose overlay 渲染 Compose。先启动 Postgres，再按顺序运行受 profile 控制的迁移和角色引导一次性任务。普通 `up` 不会自动运行它们；服务每次重启也不会触发重新迁移。

```sh
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml up -d postgres

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile directory-bootstrap -f compose.yaml run --rm directory-migrate

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile directory-bootstrap -f compose.yaml run --rm directory-runtime-role-bootstrap
```

### Cold start: connector bootstrap before Navigator / 冷启动：先配置连接器，再启动 Navigator

Set `ARTIFACTS`, `OWNER_AAD_ID`, `EXPECTED_ISSUER`, `ORGANIZATION_ID`, and `WORKSPACE_ID` in the operator shell from the verified private deployment config. Do not source credential files or print their contents.

操作前从已核实的私有部署配置中，在运维 shell 设置 `ARTIFACTS`、`OWNER_AAD_ID`、`EXPECTED_ISSUER`、`ORGANIZATION_ID` 和 `WORKSPACE_ID`。不要 source 凭据文件，也不要打印其内容。

Provision the private `config/` and `secrets/` directories for the fixed container owner UID `10001` with mode `0700`. Every Compose-mounted config file must be owned by UID `10001`, mode `0400` or `0600`, and readable by that owner. The preflight checks these facts; an operator-owned `0600` file cannot be read by Navigator, Exchange, or Authority and is rejected. Runtime env/build receipts may remain with the host operator, but the mounted config/secrets directory tree is service-owned.

全量预检前，先为容器固定运行用户 UID `10001` 配置私有 `config/` 与 `secrets/` 目录，权限 `0700`。每个 Compose 挂载配置文件都必须归 UID `10001` 所有，权限为 `0400` 或 `0600`，且 owner 可读。预检会检查这些条件；运维用户所有的 `0600` 文件对 Navigator、Exchange 或 Authority 不可读，因此会被拒绝。runtime env/build 回执可以由主机运维用户持有，但挂载到服务的 config/secrets 目录树属于服务账号。

Create the bootstrap-only config files as the runtime UID, then edit the non-secret bootstrap values through the root editor. Fill only the verified owner/workspace, GitHub App installation and repository IDs, stable idempotency keys, and HF target aliases. The file contains no tokens. Empty bindings and catalog files are only for dependency bootstrap; both are rejected by every staging, stage, and cutover preflight.

```sh
sudo -n install -d -o 10001 -g 10001 -m 0700 \
  "$ARTIFACTS/config" "$ARTIFACTS/secrets" \
  "$ARTIFACTS/onboarding" "$ARTIFACTS/onboarding/private"
sudo -n install -o 10001 -g 10001 -m 0600 \
  tooling/deploy/teams-cloud/config/connector-bindings.empty.template.json \
  "$ARTIFACTS/config/connector-bindings.json"
sudo -n install -o 10001 -g 10001 -m 0600 \
  tooling/deploy/teams-cloud/config/connector-tool-catalog.empty.template.json \
  "$ARTIFACTS/config/connector-tool-catalog.json"
sudo -n install -o 10001 -g 10001 -m 0600 \
  tooling/deploy/teams-cloud/config/private-connector-bootstrap.template.json \
  "$ARTIFACTS/config/connector-bootstrap.json"
sudo -n -e "$ARTIFACTS/config/connector-bootstrap.json"
```

使用 `sudo install -o 10001 -g 10001 -m 0700 -d "$ARTIFACTS/config" "$ARTIFACTS/secrets"` 创建私有目录，再使用 `sudo install -o 10001 -g 10001 -m 0600` 将模板复制为 `connector-bindings.json`、`connector-tool-catalog.json`、`connector-bootstrap.json`。通过受控的 root 编辑流程填入非敏感身份/资源范围值。不要把 secret 值写进这些配置 JSON。空绑定与工具目录只用于依赖服务初始化；staging、各阶段和切换预检都会拒绝它们。

After the Directory migrations and role bootstrap above, add the initial owner using the dedicated operator login. The command creates the initial dynamic Teams subject projection. Then run `--mode bootstrap`: it validates the locked images, private config, TLS identities, host capacity, and dependency-stage provider allowlist, but returns `BOOTSTRAP_ONLY_NOT_FULL_READY` and never claims the task stack is ready.

完成上面的 Directory migration 与角色初始化后，用专用 operator 登录增补首位 owner。此命令会创建 Teams 动态主体投影。随后运行 `--mode bootstrap`：它核验锁定镜像、私有配置、TLS 身份、主机容量和依赖阶段 Provider allowlist，但只返回 `BOOTSTRAP_ONLY_NOT_FULL_READY`，不会宣称任务栈已就绪。

```sh
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile directory-operator -f compose.yaml run --rm --no-deps directory-operator \
  add --subject "$OWNER_AAD_ID" --reason "Initial Teams cloud owner"

sudo -n python3 tooling/deploy/teams-cloud/preflight.py \
  --mode bootstrap \
  --compose-file tooling/deploy/teams-cloud/compose.yaml \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --image-lock "$ARTIFACTS/build/image-lock.json" \
  --secrets-dir "$ARTIFACTS/secrets" \
  --config-dir "$ARTIFACTS/config"
```

Start only the services required to authorize and create profiles. Do not start Navigator, the Bot ingress, or public Caddy at this point. The explicit service list starts Postgres dependencies only where needed and has no published host port.

只启动授权和建立 profile 所需的服务。此时不要启动 Navigator、Bot ingress 或公网 Caddy。显式服务列表只按依赖关系启动 Postgres 等服务，且不会发布宿主机端口。

```sh
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml up -d workspace-authority account-center github-issues huggingface teams-provider exchange
```

Use the Exchange one-shot to produce a plan. It obtains a fresh Directory grant and returns the actual stable profile references and connection UUIDs without linking an account or writing a connection. Read the output privately, then add the exact returned GitHub and HF `profileRef` values to Account Center's private `workspace_operator_profile_refs` list. Recreate Account Center so the explicit shared-profile opt-in is active before the actual link.

使用 Exchange 一次性命令生成计划。它会申请新的 Directory grant，并返回真实稳定的 profile reference 和 connection UUID，但不会链接账号或写入 connection。私下查看输出后，将其中精确的 GitHub、HF `profileRef` 添加到 Account Center 私有配置的 `workspace_operator_profile_refs` 列表；然后重建 Account Center，使显式共享 profile 授权生效，再执行实际链接。

```sh
install -d -m 700 "$ARTIFACTS/acceptance"
umask 077
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml run --rm --no-deps exchange integrations bootstrap-connectors \
  --integration-database /data/integrations.sqlite3 \
  --private-config /run/config/connector-bootstrap.json \
  --plan-only > "$ARTIFACTS/acceptance/connector-bootstrap-plan.json"

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml up -d --force-recreate account-center
```

After verifying the planned refs and the active Account Center profile allowlist, run the same bootstrap once without `--plan-only`. Verify the returned `directoryRoleVerified`, healthy linked profiles, and connection IDs. Then export the real launcher map for the exact issuer, owner subject, and workspace to a same-directory temporary file and atomically replace `connector-bindings.json`. The exporter reads Exchange's durable SQLite rows; do not hand-create or guess connection IDs. Next, call the private Exchange `connector-tool-catalog` exporter. It queries the actual configured provider runtimes through Exchange's mTLS clients and returns only their tool schemas and safe metadata; it is a local CLI operation, not a debug HTTP endpoint. Replace `connector-tool-catalog.json` atomically with that output. Stage-1 preflight requires a non-empty matching GitHub/HF binding map and tool catalog; each tool ID and read/write bit must agree with the fixed binding policy.

核对计划引用以及 Account Center 已加载的 profile allowlist 后，再执行一次相同 bootstrap，移除 `--plan-only`。检查返回的 `directoryRoleVerified`、已链接且健康的 profile 和 connection ID。随后以精确 issuer、owner subject 和 workspace 导出 launcher map 到同目录临时文件，再原子替换 `connector-bindings.json`。导出器读取 Exchange 持久 SQLite 记录；不要手工创建或猜测 connection ID。接着调用 Exchange 私有 `connector-tool-catalog` exporter：它通过 Exchange mTLS 客户端查询实际配置的 Provider runtime，只返回工具 schema 和安全元数据；这是本地 CLI 操作，不是 debug HTTP endpoint。将输出原子替换到 `connector-tool-catalog.json`。Stage 1 预检要求 GitHub/HF 非空绑定与工具目录匹配；每个 tool ID 和读写标记必须符合固定绑定策略。

```sh
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml stop exchange

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml run --rm --no-deps exchange integrations bootstrap-connectors \
  --integration-database /data/integrations.sqlite3 \
  --private-config /run/config/connector-bootstrap.json

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml up -d exchange

umask 077
temporary="$(mktemp)"
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml exec -T exchange \
  /app/Cyrene-Services/Cyrene-Exchange/product/.venv/bin/cyrene-exchange integrations connector-bindings \
  --integration-database /data/integrations.sqlite3 \
  --issuer "$EXPECTED_ISSUER" --subject "$OWNER_AAD_ID" \
  --workspace-id "$WORKSPACE_ID" > "$temporary"
sudo -n install -o 10001 -g 10001 -m 0600 "$temporary" \
  "$ARTIFACTS/config/.connector-bindings.json.tmp"
sudo -n mv -f "$ARTIFACTS/config/.connector-bindings.json.tmp" \
  "$ARTIFACTS/config/connector-bindings.json"
rm -f "$temporary"

temporary="$(mktemp)"
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml exec -T exchange \
  /usr/local/bin/cyrene-secret-env-entrypoint \
  /app/Cyrene-Services/Cyrene-Exchange/product/.venv/bin/cyrene-exchange integrations connector-tool-catalog \
  --integration-database /data/integrations.sqlite3 \
  --issuer "$EXPECTED_ISSUER" --subject "$OWNER_AAD_ID" \
  --organization-id "$ORGANIZATION_ID" --workspace-id "$WORKSPACE_ID" > "$temporary"
sudo -n install -o 10001 -g 10001 -m 0600 "$temporary" \
  "$ARTIFACTS/config/.connector-tool-catalog.json.tmp"
sudo -n mv -f "$ARTIFACTS/config/.connector-tool-catalog.json.tmp" \
  "$ARTIFACTS/config/connector-tool-catalog.json"
rm -f "$temporary"
```

Run the stage-1 preflight only after the map is present, then start Navigator Persistence, Navigator, and the loopback-only staging ingress. This is the first point where Teams callbacks can reach Navigator. A `bootstrap` preflight result is never interchangeable with the full stage-1/staging gate.

只有映射文件实际生成后才运行 Stage 1 预检，然后启动 Navigator Persistence、Navigator 和仅 loopback 监听的 staging ingress。这是 Teams 回调首次能够到达 Navigator 的步骤。`bootstrap` 预检结果不能替代完整 Stage 1/staging gate。

```sh
sudo -n python3 tooling/deploy/teams-cloud/preflight.py \
  --mode stage1 \
  --compose-file tooling/deploy/teams-cloud/compose.yaml \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --image-lock "$ARTIFACTS/build/image-lock.json" \
  --secrets-dir "$ARTIFACTS/secrets" \
  --config-dir "$ARTIFACTS/config"

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile public-ingress -f compose.yaml up -d \
  navigator-persistence navigator teams-ingress teams-public-ingress
```

The runtime uses separate private mTLS DirectPluginRuntime services for Account Center, GitHub, Hugging Face, Teams replies, and (from stage 2) cloud management. Exchange is the only caller network peer for each provider; GitHub and Hugging Face reach Account Center over separate private networks. The Teams ingress sidecar validates Bot Framework callbacks and forwards signed events to Navigator, while the separate Teams provider sends replies through Exchange. The Bot client secret is mounted only into the Teams provider process. Navigator persistence, Account Center state, Exchange integration state, Directory Postgres, and Caddy certificate state use durable volumes.

Navigator connects to Exchange and Navigator Persistence over private HTTPS on their existing service ports. The two server leaves are issued by the dedicated internal service CA and carry the `exchange` and `navigator-persistence` DNS SANs. Navigator receives the CA as a read-only file; its Node clients trust the CA additively, while the launcher combines it with system roots for Python HTTP clients. Exchange and Persistence health checks verify the same TLS name and root. Listener certificates and private keys are mounted only into their owning service.

The Teams ingress sidecar forwards its HMAC-authenticated event envelope to Navigator over the private Docker network using the connector's explicitly supported private-service HTTP mode. That route has no public port; only the Bot callback path is published through Caddy.

运行拓扑通过独立的私有 mTLS DirectPluginRuntime 服务承载 Account Center、GitHub、Hugging Face、Teams 回复以及（stage 2 起）云管理。Exchange 是每个 Provider 的唯一调用网络对端；GitHub 与 Hugging Face 通过各自独立内网访问 Account Center。Teams ingress sidecar 校验 Bot Framework 回调并向 Navigator 转发签名事件，独立的 Teams provider 则经 Exchange 发送回复。Bot client secret 只挂载给 Teams provider 进程。Navigator 持久化、Account Center 状态、Exchange 集成状态、Directory Postgres 与 Caddy 证书状态都使用持久卷。

Navigator 通过现有服务端口的私有 HTTPS 连接 Exchange 和 Navigator Persistence。两个服务端 leaf 证书由专用 internal service CA 签发，并分别含 `exchange` 和 `navigator-persistence` DNS SAN。CA 以只读文件交给 Navigator；Node 客户端以附加根的方式信任它，launcher 为 Python HTTP 客户端把该根合并到系统 CA bundle。Exchange 与 Persistence 的健康检查也会校验同一 TLS 名称和根证书。监听证书和私钥只挂载到对应服务。

Teams ingress sidecar 通过私有 Docker 网络，以 Connector 明确支持的私有服务 HTTP 模式向 Navigator 发送经 HMAC 认证的事件 envelope。该路由不发布宿主端口；只有 Bot 回调路径通过 Caddy 对外发布。

Run the staged service profiles only after their preceding acceptance receipts are verified. Stage 2 adds the Azure/GCP overlay. Stage 3 includes both overlay files but enables only the `runpod` profile; it reconfigures the same `cloud-management-phase2` service and keeps one writer for the durable provider state volume.

```sh
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile public-ingress -f compose.yaml up -d

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile public-ingress --profile cloud-phase2 \
  -f compose.yaml -f compose.cloud-phase2.yaml up -d

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile public-ingress --profile runpod \
  -f compose.yaml -f compose.cloud-phase2.yaml -f compose.runpod.yaml up -d
```

只有前一阶段回执已核验后，才启用下一阶段服务 profile。Stage 2 增加 Azure/GCP overlay。Stage 3 加载两个 overlay 文件，但只启用 `runpod` profile；它改为配置同一个 `cloud-management-phase2` 服务，并让持久云 Provider 状态卷保持唯一写入者。

```sh
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile public-ingress -f compose.yaml up -d

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile public-ingress --profile cloud-phase2 \
  -f compose.yaml -f compose.cloud-phase2.yaml up -d

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile public-ingress --profile runpod \
  -f compose.yaml -f compose.cloud-phase2.yaml -f compose.runpod.yaml up -d
```

The staged Caddy file uses an internal certificate and a high loopback-only HTTPS port. Only `teams-public-ingress` may publish a host port; the public route accepts `POST /api/messages` and rejects every other path or method. Navigator, Directory Authority, Exchange, Account Center, and providers remain on private Compose networks. Production publishes TCP 443 only, uses ACME TLS-ALPN, disables the HTTP-01 challenge, and leaves host port 80 unchanged and closed. The production preflight rejects `Caddyfile.staging` and `tls internal`. Bind to the public NIC address only after DNS, the Bot callback URL, and host port checks are ready. Preserve the old Docker stack during staging and rollback by stopping only the new ingress before restoring the old listener.

Staging Caddy 使用内部证书和仅监听 loopback 的高位 HTTPS 端口。只有 `teams-public-ingress` 可以映射宿主机端口；公网路由只接受 `POST /api/messages`，其他路径或方法均拒绝。Navigator、Directory Authority、Exchange、Account Center 和 Provider 仅在私有 Compose 网络中运行。生产只映射 TCP 443，使用 ACME TLS-ALPN，禁用 HTTP-01 challenge，并保持宿主机 80 端口不变且关闭。生产预检会拒绝 `Caddyfile.staging` 和 `tls internal`。只有 DNS、Bot 回调 URL 和宿主机端口检查就绪后，才绑定公网 NIC 地址。Staging 期间保留旧 Docker 栈；回滚时只停止新 ingress，再恢复旧监听。

## Connector stages and budget / 连接器阶段与预算

1. **Teams, GitHub, Hugging Face.** Use the service-owned GitHub App key through Account Center. Hugging Face uses a newly authorized device-OAuth bootstrap file; never copy the old personal OAuth token file. Keep the bootstrap under private onboarding storage and mount it read-only into Account Center only. Account Center persists profile credentials encrypted in its durable database; keep the source owner-only for restarts and reauthentication until a later config rollout removes its file reference and mount. Generate the Exchange connector map and live provider tool catalog from the verified owner/workspace. Verify private reads, a bounded API write, read-back, and deletion before enabling cloud-provider profiles.
2. **Azure and GCP.** Add only Azure and GCP profiles using the dedicated User Assigned Managed Identity and the GCP Workload Identity Federation config. Use the formal DSH → Exchange → DirectPlugin API path for the identity check, enumeration, detail, bounded create/lifecycle, status read-back, and cleanup. Native vendor CLIs may make read-only queries against those API-created resources; do not create a second copy through CLI.
3. **RunPod Pods.** Add the API key and RunPod profile only after the Azure/GCP API evidence is `VERIFIED`. Test Pod lifecycle, logs, readiness, and deletion through the formal API path. CLI probes remain read-only. Serverless is out of this stage.

1. **Teams、GitHub、Hugging Face。** 通过 Account Center 使用服务专用 GitHub App 密钥。Hugging Face 使用新授权的 device-OAuth bootstrap 文件；不得复制旧个人 OAuth token 文件。bootstrap 文件只保存在私有 onboarding 目录，只读挂载到 Account Center。Account Center 会将 profile 凭据加密保存到持久数据库；在后续配置发布同时移除文件引用和挂载前，需将源文件保持 owner-only，以支持重启和重新授权。用已核验 owner/workspace 导出 Exchange 连接映射和真实 Provider 工具目录。依次验收私有读取、受限 API 写入、回读和删除，然后才开放云 Provider profile。
2. **Azure、GCP。** 仅增加 Azure 与 GCP profile，使用专用 User Assigned Managed Identity 和 GCP Workload Identity Federation 配置。身份、资源枚举、详情、受限创建/生命周期、状态回读和清理均走正式 DSH → Exchange → DirectPlugin API；原生供应商 CLI 只对 API 创建的资源执行只读查询，不重复创建资源。
3. **RunPod Pods。** Azure/GCP API 证据标记为 `VERIFIED` 后再提供 API key 并添加 RunPod profile。通过正式 API 路径验收 Pod 生命周期、日志、就绪状态和删除；CLI 探测保持只读。Serverless 不在此阶段。

GitHub and Hugging Face keep structured API tools for common reads and writes, plus separate fixed-binary CLI tools (`github.vendor.cli.execute` and `huggingface.vendor.cli.execute`). The ARM64 GitHub CLI v2.102.1 archive is pinned by SHA-256 `5019b3cc6e8a03bb4d88600f8dbda2add8e6407705c159b0c63151036e77a1af`; the Hugging Face `hf` CLI comes from `huggingface_hub==2.2.0` with the Python 3.12 ARM64 transitive lock. Each CLI process receives only its profile-scoped short-lived credential, runs in a temporary home/workdir, and returns a bounded command receipt. It does not load a personal `gh`/`hf` config or return raw command output to chat.

GitHub 和 Hugging Face 保留常用读写的结构化 API 工具，同时另提供固定二进制 CLI 工具（`github.vendor.cli.execute`、`huggingface.vendor.cli.execute`）。ARM64 GitHub CLI v2.102.1 发布包固定 SHA-256 `5019b3cc6e8a03bb4d88600f8dbda2add8e6407705c159b0c63151036e77a1af`；Hugging Face `hf` CLI 来自 `huggingface_hub==2.2.0`，并使用 Python 3.12 ARM64 传递依赖锁。每个 CLI 子进程只获得该 profile 范围的短期凭据，运行在临时 HOME/工作目录，并返回有界命令回执。它不会读取个人 `gh`/`hf` 配置，也不会将原始命令输出返回聊天。

Cloud Management installs three ARM64 vendor CLIs into the fixed paths expected by the provider profiles: Azure CLI `2.91.0` (`azure-cli_2.91.0-1~bookworm_arm64.deb`, SHA-256 `dcf43fcd7f4d538cf31c958e797e40be3e651dcacbb90de8c3aa2ab642a5ab0d`), Google Cloud CLI `590.0.1` (SHA-256 `c9e8618c9257b3289deb7cb5483479230d5d96230bd68a80fce17f592d0d3012`), and RunPod CLI `3.0.1` (SHA-256 `7d75577b52950e40e1fbf6b34b8e1302d3d408e2f1d8637b5031c0f0b1089beb`). The build verifies Microsoft’s signed Bookworm ARM64 package index and signing-key fingerprint before installing Azure CLI; the two archives are checked against their pinned digests. The versions and digests are repeated in the image labels and build plan. See the [Microsoft ARM64 apt installation guidance](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-linux), [Google Cloud CLI download documentation](https://docs.cloud.google.com/sdk/docs/install-sdk), and [RunPod CLI v3.0.1 release](https://github.com/runpod/runpodctl/releases/tag/v3.0.1).

Cloud Management 会把三个供应商 ARM64 CLI 安装到 profile 固定路径：Azure CLI `2.91.0`（`azure-cli_2.91.0-1~bookworm_arm64.deb`，SHA-256 `dcf43fcd7f4d538cf31c958e797e40be3e651dcacbb90de8c3aa2ab642a5ab0d`）、Google Cloud CLI `590.0.1`（SHA-256 `c9e8618c9257b3289deb7cb5483479230d5d96230bd68a80fce17f592d0d3012`）和 RunPod CLI `3.0.1`（SHA-256 `7d75577b52950e40e1fbf6b34b8e1302d3d408e2f1d8637b5031c0f0b1089beb`）。构建时先核对 Microsoft 已签名的 Bookworm ARM64 包索引及签名密钥指纹，再安装 Azure CLI；两个压缩包都按固定摘要校验。版本和摘要同时记录在镜像 labels 与 build plan 中。参考 [Microsoft ARM64 apt 安装说明](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-linux)、[Google Cloud CLI 下载文档](https://docs.cloud.google.com/sdk/docs/install-sdk) 和 [RunPod CLI v3.0.1 发布页](https://github.com/runpod/runpodctl/releases/tag/v3.0.1)。

The acceptance runner accepts only argv arrays for `az`, `gcloud`, or `runpodctl`; it has no shell mode or credential arguments. Store each resource's task/owner marker, provider, direct quote URL/time, USD upper bound, creation action, operation read-back, exact vendor reference, and deletion read-back in a private plan/ledger. Reuse the same task ID and owner for the Azure/GCP and RunPod plans. RunPod preflight adds the already verified Azure/GCP ledger reservation to the new quote and refuses any cumulative total above USD 5. Each resource's worst-case create, exercise, read-back, and cleanup window is capped at 900 seconds, and resources are cleaned one at a time. The runner records `CREATE_STARTED` before create, reconciles `UNKNOWN` by read-back instead of recreating, and attempts deletion plus final absence verification for every resource even after failure, timeout, or cancellation. Stopping a VM or Pod does not count as cleanup.

验收 runner 只接受 `az`、`gcloud` 或 `runpodctl` 的 argv 数组，不提供 shell 模式，也不接受命令行凭据。私有计划/台账记录每个资源的 task/owner marker、Provider、直接报价链接和时间、USD 上限、创建动作、操作回读、精确供应商引用及删除回读。Azure/GCP 与 RunPod 阶段沿用相同 task ID 和 owner；RunPod 预检会把已验证的 Azure/GCP 台账预留额加到新报价上，累计超过 5 美元即拒绝。每个资源从创建、操作、回读到删除的最坏时间不得超过 900 秒，runner 会逐项创建和清理。创建前先记录 `CREATE_STARTED`；`UNKNOWN` 通过回读处理，不会重建；即使失败、超时或取消，也会继续尝试每项资源删除并做最终缺失回读。停止 VM 或 Pod 不等同于资源清理。

Store the GitHub/HF receipt from `acceptance-evidence.template.json` and typed API receipts from `cloud-api-evidence.azure-gcp.template.json` and `cloud-api-evidence.runpod.template.json` under the private artifact directory, owned by UID `10001` with mode `0600`. Before entering stage 2, record verified GitHub/HF identity, private reads, a test write/read-back, and cleanup absence. The Azure/GCP API receipt must hash the exact raw GitHub/HF receipt; the RunPod API receipt must hash the exact raw Azure/GCP API receipt. The API receipts are accepted only when their execution path is the Navigator DSH → Exchange → DirectPluginRuntime API, each resource has an independent cleanup absence read-back, and the one shared API reservation ledger stays within USD 5. This task uses the typed API path for every billable lifecycle write. Native vendor CLIs may issue read-only probes against the API-created resources, but this task does not use CLI commands to create, update, stop, or delete resources. Keep the same task ID and owner across stages. Use fresh, provider-specific prices before the first write. No provider write or vendor-billable resource was created while preparing this bundle.

GitHub/HF 回执使用 `acceptance-evidence.template.json`；正式 API 回执使用 `cloud-api-evidence.azure-gcp.template.json` 和 `cloud-api-evidence.runpod.template.json`，并写入私有 artifacts，文件 UID `10001`、权限 `0600`。进入 Stage 2 前，先记录已验证身份、私有读取、测试写入与回读，以及清理缺失回读。Azure/GCP API 回执须绑定 GitHub/HF 原始回执摘要；RunPod API 回执须绑定 Azure/GCP API 原始回执摘要。只有正式 Navigator DSH → Exchange → DirectPluginRuntime API 路径、每个资源独立清理缺失回读以及同一个 API 预算账本累计不超过 5 美元时，API 回执才有效。本任务所有计费生命周期写入均走 typed API；原生供应商 CLI 只可查询 API 创建的资源，不可通过 CLI 创建、更新、停止或删除资源。所有阶段必须沿用同一 task ID 和 owner。首次写入前核对最新供应商报价。准备此部署包期间没有创建供应商资源或执行计费写入。

`api_acceptance_budget.py` is a library-only write-ahead ledger for the existing private DSH coordinator. The caller records the quote/resource reservation, then persists `begin_mutation` before it dispatches a typed tool call through DSH. It records request/result digests and the fresh grant, Work, and provider operation references, and blocks another write while the prior outcome is unresolved. The resource window is capped at 900 seconds; the final 300 seconds are reserved for cleanup and an independent absence read-back, so the last non-cleanup write must finish before that cleanup window begins. An unknown create must be reconciled against the exact recorded target; this library never retries or calls a vendor. `finalize` can emit `REAL_PROVIDER_API` evidence only after every stage resource has a successful typed write read-back and verified absence. The bundle does not include the private DSH coordinator itself, and preparing it made no provider API write.

`api_acceptance_budget.py` 是供既有私有 DSH 协调器调用的纯写前账本。调用方先记录报价/资源预算预留，再持久化 `begin_mutation`，之后才通过 DSH 派发 typed tool call。账本记录请求/结果摘要及新的 grant、Work 和 Provider operation reference；前一调用结果未核实前会拒绝新的写入。资源窗口最多 900 秒；最后 300 秒留给清理和独立缺失回读，因此所有非清理写入都必须在清理窗口开始前结束。创建结果未知时，必须按账本精确记录的目标回读核对；该库不会自动重试，也不会调用供应商。只有本阶段所有资源均有成功的 typed 写入回读和已验证缺失状态后，`finalize` 才能生成 `REAL_PROVIDER_API` 回执。此部署包不包含私有 DSH 协调器本身，准备工作也没有执行供应商 API 写入。

The ledger records and rejects timing violations, but it cannot interrupt an external DSH request or guarantee vendor completion time by itself. The private coordinator must enforce a 900-second watchdog, start final deletion by the 600-second cutoff, and keep reconciling uncertain writes through the same typed API path. If dispatch, read-back, or cleanup exceeds the deadline, retain the reservation, finish safe cleanup, record the window breach, and leave the stage `NOT_RUN`/failed; do not change it to `VERIFIED`.

账本能记录并拒绝超时状态，但不能自行中断外部 DSH 请求，也不能保证供应商完成时间。私有协调器必须设置 900 秒 watchdog，最迟在第 600 秒开始最终删除，并通过同一 typed API 路径持续核对不确定写入。如果派发、回读或清理超时，应保留预算预留、继续安全清理、记录超时，并将阶段保持为 `NOT_RUN`/失败；不得改为 `VERIFIED`。

Stage 3 preflight additionally requires the verified Azure/GCP API receipt. Cloud cutover requires the same Stage 2 evidence; if the RunPod profile is enabled, it also requires the RunPod API receipt. The single API ledger reserves cost before dispatch and carries the exact previous-receipt hash. This task rejects the legacy vendor-CLI resource-mutation plan; its read-only CLI probes add no separate resource reservation. Pass the exact receipt paths explicitly so a stale or unrelated file cannot satisfy the gate:

Stage 3 预检还需要已验证的 Azure/GCP API 回执。公网切换需要相同 Stage 2 证据；启用 RunPod profile 时还需要 RunPod API 回执。唯一 API 账本会在派发前预留成本，并绑定精确的前序回执摘要。本任务拒绝旧版供应商 CLI 资源变更计划；只读 CLI 探测不另建资源预算。

```sh
sudo -n python3 tooling/deploy/teams-cloud/preflight.py \
  --mode stage3 \
  --compose-file tooling/deploy/teams-cloud/compose.yaml \
  --compose-file tooling/deploy/teams-cloud/compose.cloud-phase2.yaml \
  --compose-file tooling/deploy/teams-cloud/compose.runpod.yaml \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --image-lock "$ARTIFACTS/build/image-lock.json" \
  --secrets-dir "$ARTIFACTS/secrets" --config-dir "$ARTIFACTS/config" \
  --github-hf-evidence "$ARTIFACTS/acceptance/github-hf.json" \
  --azure-gcp-api-evidence "$ARTIFACTS/acceptance/provider-plans/azure-gcp-api-evidence.json"

sudo -n python3 tooling/deploy/teams-cloud/preflight.py \
  --mode cutover \
  --compose-file tooling/deploy/teams-cloud/compose.yaml \
  --compose-file tooling/deploy/teams-cloud/compose.cloud-phase2.yaml \
  --compose-file tooling/deploy/teams-cloud/compose.runpod.yaml \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --image-lock "$ARTIFACTS/build/image-lock.json" \
  --secrets-dir "$ARTIFACTS/secrets" --config-dir "$ARTIFACTS/config" \
  --github-hf-evidence "$ARTIFACTS/acceptance/github-hf.json" \
  --azure-gcp-api-evidence "$ARTIFACTS/acceptance/provider-plans/azure-gcp-api-evidence.json" \
  --runpod-api-evidence "$ARTIFACTS/acceptance/provider-plans/runpod-api-evidence.json"
```

The Cloud Management image retains pinned `az`, `gcloud`, and `runpodctl` binaries for the provider's native CLI tool. This deployment task uses that tool only for read-only probes against API-created resources. CLI stdout/stderr is suppressed by the provider, so a command-completed receipt is not a resource-state proof; use the typed API's independent read-back and cleanup evidence. The legacy `acceptance_budget.py` CLI resource-mutation runner remains for unrelated task IDs, but it explicitly rejects `teams-cloud-20261023` to prevent a second, uncoordinated reservation path.

Cloud Management 镜像仍包含固定版本的 `az`、`gcloud` 和 `runpodctl`，供 Provider 原生命令工具使用。本部署任务只允许通过 CLI 对 API 创建的资源进行只读查询。Provider 会抑制 CLI stdout/stderr，因此命令完成回执不等于资源状态证明；状态和清理证据必须来自 typed API 的独立回读。旧版 `acceptance_budget.py` CLI 资源变更 runner 仍可供其他 task ID 使用，但会明确拒绝 `teams-cloud-20261023`，避免出现第二条互不协调的预算预留路径。

## Owner maintenance / Owner 维护操作

After the initial owner projection is installed, use the `directory-operator` profile only for an explicit one-shot membership change. Do not pass the admin URL to this command.

初始 owner 投影安装后，仅使用 `directory-operator` profile 执行明确的一次性成员变更。该命令不得接收 admin URL。

```sh
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile directory-operator -f compose.yaml run --rm --no-deps directory-operator \
  add --subject "$AAD_OBJECT_ID" --reason "<short audit reason>"
```

Use `revoke` for removal. A successful CLI call updates the private subject projection atomically; Navigator reads the replacement from its read-only directory mount on the next request without a rebuild or restart. Do not hand-edit the projection to grant access, and do not report the change as complete if the wrapper reports a projection-write failure.

撤销成员时将 `add` 改为 `revoke`。CLI 成功后包装器会原子更新私有身份投影；Navigator 下次请求即可从只读目录挂载读取新文件，无需重建或重启。不要手工编辑投影以授予权限；若包装器报告投影写入失败，不得宣称变更完成。
