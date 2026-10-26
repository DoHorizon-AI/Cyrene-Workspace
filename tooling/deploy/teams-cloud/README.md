# Teams cloud deployment bundle / Teams 云端部署包

This directory contains the guarded ARM64 build, runtime composition, host preflight,
and bounded cloud-acceptance helpers for the Cyrene Teams workload.

本目录提供 Cyrene Teams 工作负载的受控 ARM64 构建、运行编排、主机预检和有预算上限的云端验收工具。

The existing nominal 4 GiB VM may report less RAM in Linux `MemTotal` after
firmware/kernel reservations. Host recognition requires at least 3584 MiB of
guest-visible total RAM; it does not waive the separate `MemAvailable`, runtime
RSS, cgroup-limit, or disk-reserve checks.

现有 4 GiB 规格云机的 Linux `MemTotal` 会扣除固件和内核保留内存。规格识别要求 guest
可见总内存至少 3584 MiB；实际 `MemAvailable`、运行时 RSS、cgroup 上限和磁盘余量检查仍须独立通过。

| File | Responsibility |
| --- | --- |
| `compose.yaml` | Private service network and the single Teams callback ingress / 内网服务与唯一 Teams 回调入口 |
| `compose.cloud-phase2.yaml`, `compose.runpod.yaml` | Opt-in Azure/GCP then RunPod on the same single cloud-provider service / 在同一个云 Provider 服务上按阶段启用 Azure/GCP 和 RunPod |
| `Dockerfile.*` | Source-built ARM64 service images with fixed runtime entrypoints / 从固定源码构建 ARM64 服务镜像并锁定运行入口 |
| `config/requirements.huggingface-cli.lock` | Hash-locked ARM64/Python 3.12 `hf` CLI dependency closure / 固定哈希的 ARM64/Python 3.12 `hf` CLI 依赖闭包 |
| `compose.huggingface.yaml`, `config/github-only.env.template` | Explicit HF-enabled overlay or auditable GitHub-only selection / 显式 HF overlay 或可审计的 GitHub-only 选择 |
| `compose.proactive-assistant-v1.yaml`, `compose.computer-runtime.yaml`, `Dockerfile.computer-runtime`, `Dockerfile.computer-proxy` | Opt-in unlimited conversational model profile, private ephemeral computer runner, and mTLS-only bridge / 可选的不限聊天次数模型配置、内网临时电脑执行容器与仅 mTLS 桥接 |
| `compose.codebuddy-acp.yaml`, `config/codebuddy-acp.env.template` | Explicit native CodeBuddy ACP profile with a closed command/model selector / 显式启用原生 CodeBuddy ACP，并限制命令和模型选择 |
| `config/responsibility-targets.v1.schema.json`, `config/responsibility-targets.v1.template.json`, `config/proactive-assistant-workflow.v2.template.json` | Host-owned repository grants and disabled Workflows v2 template / 宿主机维护的仓库授权与默认禁用的 Workflows v2 模板 |
| `config/computer-runner.integration-fragment.v1.template.json` | Non-secret Exchange host binding fragment for the mTLS computer runner / 电脑执行器 mTLS 连接器的非敏感 Exchange 宿主绑定片段 |
| `generate_github_workflow_allowlist.py` | Derive matching private GitHub provider and Navigator rerun grants from one verified nine-repository inventory / 从同一份九仓库核实清单生成匹配的 GitHub Provider 与 Navigator 重跑授权 |
| `config/private-connector-bootstrap.github-only.template.json` | Exchange GitHub-only bootstrap input with no HF profile or credential ref / 不含 HF profile 和凭据引用的 Exchange GitHub-only bootstrap 输入 |
| Generated `.artifacts/<task>/config/github-issues-source-workflow-ids.v1.json` | Owner-only provider configuration fragment; merge its `source_workflow_ids` into the complete private GitHub provider configuration / 仅 owner 可读的 Provider 配置片段；将 `source_workflow_ids` 合并到完整私有 GitHub Provider 配置 |
| `config/antigravity-model-only-profile.template.md`, `config/proactive-assistant-v1.env.template` | Model-only CLI profile and Antigravity/CodeBuddy runtime defaults / 仅模型 CLI profile 与 Antigravity/CodeBuddy 运行时默认值 |
| `prepare_production_runtime.py` | Produce a production-only DSH/Navigator dependency closure without development caches / 构建不含开发缓存的 DSH/Navigator 生产依赖集合 |
| `config/acceptance-plan.schema.json`, `config/acceptance-plan*.template.json`, `config/acceptance-evidence.template.json`, `config/cloud-api-evidence.*` | USD 5 cumulative reservations, closed real-API stage receipts, and exact cleanup actions / 5 美元跨阶段预算、正式 API 阶段回执和精确清理动作 |
| `config/connector-bindings*.json`, `config/connector-tool-catalog*.json` | Bootstrap-only empty maps plus production schemas for Exchange-issued bindings and live provider tool catalogs / 仅限引导阶段的空映射，以及 Exchange 绑定与实时 Provider 工具目录的生产 Schema |
| `build_arm64.py` | Fetch exact remote commits, build ARM64 images, and record image IDs and materialized disk footprints / 获取精确远端提交、构建 ARM64 镜像并记录镜像 ID 与落盘占用 |
| `transfer_arm64.py` | Enforce strict SSH/capacity gates and stream locked images directly into Docker / 严格核验 SSH 和容量门槛，并将锁定镜像直接流式导入 Docker |
| `preflight.py` | Read-only host, image, config, secret, stage-evidence, tool-catalog, and ingress checks / 只读核验主机、镜像、配置、密钥、阶段回执、工具目录和入口 |
| `runtime_update.py` | Owner-confirmed, drain-aware Navigator/Exchange update plan, apply, rollback recovery, and status / 需 owner 明确应用的 Navigator/Exchange 排空热更新计划、回滚恢复与状态查询 |
| `host_fronted_canary.py` | Read-only readiness gate for an existing host Caddy front and the private locked ingress / 现有宿主机 Caddy 与锁定私有 ingress 的只读就绪门禁 |
| `plugin_grpc_health_probe.py` | Verify private DirectPluginRuntime mTLS health and plugin identity / 通过内网 mTLS 核验 DirectPluginRuntime 健康状态与插件身份 |
| `acceptance_budget.py` | Legacy CLI resource lifecycle runner for unrelated task IDs; rejects CLI mutation plans for this task / 供其他 task ID 使用的旧版 CLI 生命周期 runner；本任务会拒绝 CLI 资源变更计划 |
| `api_acceptance_budget.py` | Reusable write-ahead USD 5 reservation/state ledger for an existing private DSH API coordinator; never invokes providers / 供既有私有 DSH API 协调器复用的 5 美元写前预留与状态账本；本模块不会调用供应商 |
| `export_authority_keyring.py` | Derive a public-only Ed25519 keyring from private seed files / 从私钥种子导出仅含公钥的 Ed25519 keyring |
| `maintainer_subjects.py` | Run one owner-scoped Directory maintainer change and atomically refresh the Teams actor projection / 执行 owner Directory 增撤并原子刷新 Teams 身份投影 |
| `config/` | Non-secret templates and lock schemas / 非敏感模板与锁文件 Schema |
| [`docs/TEAMS_CLOUD_DEPLOYMENT.md`](../../../docs/TEAMS_CLOUD_DEPLOYMENT.md) | Bilingual staging, identity, provider order, rollback, and evidence runbook / 双语 staging、身份、Provider 顺序、回滚和证据手册 |

Run the source build only from a clean, committed Workspace checkout and provide
the exact source SHA for every repository. The builder snapshots remote Git
commits into a temporary directory; it never copies files from sibling working
trees or a user's home directory.

`runtime.env.template` defaults `CYRENE_HF_ENABLED=true`; the HF-enabled path requires `compose.huggingface.yaml`, the real owner-only OAuth seed, current HF binding, and the live HF tool catalog. For GitHub-only bootstrap or Stage 1 staging, load `config/github-only.env.template` after `runtime.env` and omit the HF Compose overlay. Use the GitHub-only Account Center config, `config/private-connector-bootstrap.github-only.template.json`, and an Exchange integration config that omits top-level `authorizationCloud` when the expected provider set is empty. Do not provide `authorizationCloud.profiles: {}`; Exchange rejects an empty `profiles` map when `authorizationCloud` is present. Later stages with expected providers retain the exact non-empty stage-scoped profile map. Preflight requires no HF provider service, source secret, provider secret references, connector binding, target, or catalog entry in GitHub-only mode. HF-disabled mode is limited to bootstrap and Stage 1/staging; later phases require the HF-enabled gates.

`runtime.env.template` 默认将 `CYRENE_HF_ENABLED=true`；启用 HF 时必须显式加入 `compose.huggingface.yaml`，并提供真实 owner-only OAuth seed、当前 HF binding 和实时 HF tool catalog。GitHub-only bootstrap 或 Stage 1 staging 应在 `runtime.env` 后加载 `config/github-only.env.template`，并省略 HF Compose overlay。使用 GitHub-only Account Center 配置和 `config/private-connector-bootstrap.github-only.template.json`；当预期 Provider 集为空时，Exchange 配置必须省略顶层 `authorizationCloud`，不能提供会被 Exchange 拒绝的空 `authorizationCloud.profiles` 映射。后续存在预期 Provider 的阶段继续使用精确的非空阶段 profile 映射。GitHub-only 模式下，预检仍要求没有 HF Provider 服务、source secret、provider secret refs、connector binding、target 或 catalog 项。HF-disabled 仅适用于 bootstrap 和 Stage 1/staging；后续阶段必须先通过 HF-enabled 门槛。

The GitHub-only cold-start order is: initialize the Directory owner; run `--mode bootstrap` with empty connector bindings/catalog and the GitHub-only bootstrap input; let Exchange produce the real GitHub profile/connection; update the GH-only Account Center allowlist; export the real GitHub and computer bindings/tool catalog; then generate the verified nine-repository workflow allowlist and replace pending target bindings. Bootstrap preflight accepts only the named pending binding/source-workflow markers and returns `BOOTSTRAP_ONLY_NOT_FULL_READY`. Stage 1 requires live bindings, the exact repository allowlist, and matching tool schemas. For CodeBuddy ACP, `CYRENE_WORKFLOWS_ENABLED` defaults to `false`; keep the host WorkFlow candidate disabled through the first owner Teams chat, then read the actual Work API and require all stored v2 definitions to be absent or disabled. The host candidate remains disabled as a preflight manifest while owner-authorized activation is persisted separately through Work API PUT and GET read-back.

GitHub-only 冷启动顺序为：先初始化 Directory owner；使用空的 connector binding/catalog 和 GitHub-only bootstrap 输入运行 `--mode bootstrap`；由 Exchange 返回真实 GitHub profile/connection；更新仅含 GitHub profile 的 Account Center allowlist；导出真实 GitHub 与 computer binding/tool catalog；再生成核实过的九仓库 workflow allowlist 并替换 pending target binding。Bootstrap 预检只接受明确列出的 pending binding/source-workflow 标记，并返回 `BOOTSTRAP_ONLY_NOT_FULL_READY`。Stage 1 要求实时 binding、精确仓库 allowlist 和匹配的 tool schema。CodeBuddy ACP 的 `CYRENE_WORKFLOWS_ENABLED` 默认是 `false`；首次 owner Teams 聊天期间保持 host WorkFlow 候选禁用，再读取实际 Work API，确认所有持久 v2 definition 均不存在或 disabled。host candidate 持续作为 disabled preflight manifest；owner 授权后的启用通过独立 Work API PUT 保存，并用 GET 回读确认。

The Antigravity and CodeBuddy ACP model profiles are optional and mutually exclusive. The CodeBuddy ACP profile uses the base file, `compose.computer-runtime.yaml`, then `compose.codebuddy-acp.yaml`; load `config/codebuddy-acp.env.template` after the private runtime env. This template selects GitHub-only mode until HF authorization is completed, defaults the Workflows engine off, selects only the native `codebuddy` CLI and `glm-5.3-flash` or `deepseek-v4.1-flash`, clears the legacy Harness model source, disables fallback, keeps two bounded foreground/background slots, and uses the service-owned `secrets/codebuddy` login directory. Its WorkFlow `modelPolicy` must use `preferredProvider=codebuddy`, the selected ACP model, and the metadata literal `fallback=configured_no_additional_spend`; the last field does not enable the disabled runtime fallback. Navigator owns the fixed ACP flags and writes each host-MCP bridge configuration to a private 0600 temporary file; Compose must not inject MCP JSON, tokens, or mounts. `CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP=true` is a required owner declaration, not a CodeBuddy CLI billing guarantee. Preflight checks the login directory's owner/mode without reading credentials or contacting CodeBuddy.

The proactive profile is not part of the default stack. Add exactly one model profile after the base Compose file and include the computer-runtime overlay for that deployment. For CodeBuddy ACP, the Workflows engine defaults off; before turning it on, read the durable Work API and verify every stored definition is absent or disabled. The host workflow file is a separate preflight manifest that stays `enabled:false`; after owner acceptance, the authorized Work API write and read-back update the durable definition. Do not activate a schedule before the host target list and owner thread/task IDs are verified. Current-VM capacity and external acceptance remain separate live checks.

Antigravity 与 CodeBuddy ACP 模型 profile 都是可选项，且不能叠加。CodeBuddy ACP 需要按顺序使用基础文件、`compose.computer-runtime.yaml`、`compose.codebuddy-acp.yaml`，并在私有 runtime env 后加载 `config/codebuddy-acp.env.template`。模板默认设置 `CYRENE_WORKFLOWS_ENABLED=false`；owner chat 和实际 Work API 定义 read-back 完成前保持关闭。workflow v2 的 `modelPolicy` 使用 `preferredProvider=codebuddy`、当前 ACP 模型，以及只作为元数据的 `fallback=configured_no_additional_spend`，这不会开启 DSH fallback。它只选择原生 `codebuddy` CLI 和 `glm-5.3-flash` 或 `deepseek-v4.1-flash`，清空旧 Harness model source、关闭运行时 fallback，并沿用服务专属的 `secrets/codebuddy` 登录目录。Navigator 固定 ACP 参数，并为每次调用在私有临时文件中写入权限为 0600 的 host-MCP bridge 配置；Compose 不得注入 MCP JSON、token 或挂载。`CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP=true` 是 owner 必填声明，不是 CodeBuddy CLI 的扣费保证。预检只验证登录目录 owner/权限，不读取凭据，也不联系 CodeBuddy。

主动助手 profile 不会进入默认服务栈。每个部署只显式添加一种模型 profile，并同时包含 computer-runtime overlay。`CYRENE_WORKFLOWS_ENABLED` 控制调度引擎；它与 Work API 中持久化的单个 definition `enabled` 状态相互独立。owner/target/connector 验收之前 definition 必须保持禁用；打开引擎前还须读回确认 Work API 中没有启用定义。当前 VM 容量和外部验收仍须独立实时核验。

The Teams v1 computer profile exposes only `computer.command.execute`, `computer.file.read`, `computer.file.write`, and `computer.directory.list`; artifact create/get tools are not exposed. Its 320 MiB total scratch budget is tmpfs-only (`/workspace` 192 MiB, artifacts 64 MiB, `/tmp` 64 MiB), so workspace files and any artifact references are lost on runner restart or replacement. Preflight reports `computerRuntimeStorage.lifetime=EPHEMERAL`, `workspaceClearedOnRestart=true`, and `artifactRefsSurviveRestart=false` when the runner is configured, or `NOT_PRESENT` when it is absent. This scratch lifecycle is separate from durable Navigator task/session/operation state and the existing service-owned credential stores. No Docker restart test was run during package preparation.

Teams v1 电脑执行 profile 只开放 `computer.command.execute`、`computer.file.read`、`computer.file.write` 和 `computer.directory.list`；不开放 artifact create/get 工具。总计 320 MiB 的 scratch 仅使用 tmpfs（`/workspace` 192 MiB、artifacts 64 MiB、`/tmp` 64 MiB），因此运行器重启或替换后工作区文件和 artifact 引用都会丢失。运行器已配置时，preflight 会报告 `computerRuntimeStorage.lifetime=EPHEMERAL`、`workspaceClearedOnRestart=true`、`artifactRefsSurviveRestart=false`；未配置时报告 `NOT_PRESENT`。这些临时目录与持久的 Navigator Task/session/operation 状态及现有服务负责的凭据存储相互独立。本轮打包准备没有运行 Docker 重启验收。

只从干净且已提交的 Workspace 检出目录执行源码构建，并为每个仓库提供精确源 SHA。构建器把远端 Git 提交快照到临时目录；不会复制同级脏工作树或用户主目录中的文件。

The base Compose file publishes only TCP 443 for the callback ingress. Exchange,
Navigator, Directory Authority, Account Center, and every DirectPluginRuntime
remain on private networks; providers use distinct service certificates, a
private client-CA bundle, and mTLS health checks. The public Caddyfile accepts
only `POST /api/messages`, uses the ACME TLS-ALPN challenge, and leaves host port
80 closed.

When HF is enabled, it uses a fresh device-OAuth bootstrap selected by the HF
Compose overlay. Its private JSON file stays outside the secret directory and
is mounted read-only into Account Center only. Account Center persists profile
credentials encrypted in its durable database; retain the bootstrap file
owner-only for restarts and reauthentication until a later config rollout
removes both the file reference and mount. The preflight validates the closed
DTO and target repository scope without printing token values. GitHub-only mode
contains no OAuth secret mount, HF provider service, HF Exchange profile,
binding, or tool catalog.

基础 Compose 文件只为回调入口发布 TCP 443。Exchange、Navigator、Directory Authority、Account Center 和所有 DirectPluginRuntime 均留在私有网络；Provider 使用各自服务证书、私有 client-CA bundle 和 mTLS 健康探针。公网 Caddyfile 只接受 `POST /api/messages`，使用 ACME TLS-ALPN challenge，并保持宿主机 80 端口关闭。

启用 HF 时，使用 HF Compose overlay 选择新的 device-OAuth bootstrap。私有 JSON 文件保存在 secrets 目录之外，只读挂载到 Account Center。Account Center 会将 profile 凭据加密保存到持久数据库；在后续配置发布同时移除文件引用和挂载前，需将 bootstrap 文件保持 owner-only，以支持重启和重新授权。预检会校验封闭 DTO 与目标仓库范围，不会输出 token 内容。GitHub-only 模式不包含 OAuth secret 挂载、HF Provider 服务、Exchange HF profile、binding 或 tool catalog。

### Runtime updater function index / 热更新函数索引

| Function | Responsibility | Shared validation used |
| --- | --- | --- |
| `create_plan` | Write a read-only plan bound to active/candidate files, running container IDs, image IDs, and service state. | Calls `preflight.run_preflight` for both input sets in `stage1` mode with the existing owned-staging-ingress flag; that path validates host/capacity, Compose, secrets, provider scope, ingress, and Stage 1 config. `preflight.run_preflight` calls `build_arm64.check_local_lock` for each lock. |
| `_resolve_plan_inputs` | Re-hash and render the reviewed inputs immediately before apply or recovery. | Apply reuses full Stage 1 preflight and `check_local_lock`; recovery reuses their configuration/image-lock checks while skipping only dynamic capacity thresholds, so a failed candidate cannot prevent restoration of the old image. |
| `_inspect_project_services`, `_verify_live_network_membership` | Read Docker project inventory, immutable IDs, health, and actual network peers. | Extends existing deployment checks with the updater's standalone-caller contract; it does not start a second service or scheduler. |
| `_validate_initial_runtime_state`, `_pause_edge`, `_drain_navigator` | Bind the fresh plan to Teams spool CAS state and drain the existing Navigator executor before replacement. | Uses Plugins spool CAS and Navigator's service-Bearer drain API; it requires complete durable counts, zero active/waiting dispatches, and a paused, idle notification worker before stop. Resume sends the producer's exact `{generation, token}` body. |
| `_update_target`, `_ensure_navigator_resumed` | Replace only `exchange` or `navigator` with `compose up --no-deps --pull never --no-build`, then read back image ID, health, and Navigator acceptance. | Reuses the plan's verified image-lock references and the runtime inventory helpers above. |
| `_rollback_operation`, `resume_operation`, `operation_status` | Drain a running candidate before rollback, restore only reviewed old image IDs, or retain a `RECOVERY_REQUIRED` journal when state is unknown. | Uses the same lock, preflight, container, network, drain, and edge-CAS checks; recovery never repeats an uncertain replacement automatically. |

`runtime_update.py` adds only the transaction sequencing and read-back checks needed for these single-service updates. Preflight remains authoritative for deployment configuration and provider/secret gates; image-lock verification remains in `build_arm64.py`.

Runtime update commands run as root. They share one root-owned mode-0700 lock directory at `/run/cyrene-runtime-update`, with root-owned mode-0600 project lock files, independent of the selected journal directory. Rootless plan/apply/status/resume is rejected so a second host UID cannot bypass the single-writer lock. The CLI updates only `navigator` and `exchange`; it never replaces the Teams ingress.

Use `plan` to bind the active deployment and candidate files, inspect the output plan and its SHA-256, then explicitly run `apply` with that digest. `status` reads the private journal. `resume` performs deterministic rollback only when no Docker mutation has an unresolved outcome. If Docker timed out or the process stopped after a persisted replacement/stop intent, `resume` reconfirms the Teams edge hold, leaves the operation in `RECOVERY_REQUIRED`, and does not repeat Compose or stop. An operator must first reconcile the exact Docker state; a transient read of the old image is not proof that a timed-out daemon request has ended.

所有 runtime update 命令必须由 root 执行。它们共用 `/run/cyrene-runtime-update` 下 root 所有、权限 0700 的锁目录，以及 root 所有、权限 0600 的项目锁文件；锁与选择的 journal 目录无关。为避免其他主机 UID 绕过单写锁，CLI 会拒绝 rootless plan/apply/status/resume。CLI 只更新 `navigator` 和 `exchange`，不会替换 Teams ingress。

先用 `plan` 绑定当前部署和候选文件，检查输出计划及其 SHA-256，再用该摘要明确执行 `apply`。`status` 读取私有 journal。只有不存在未决 Docker 操作时，`resume` 才会执行确定性回滚。如果 Docker 超时，或进程在已持久化替换/停止意图后退出，`resume` 会重新确认 Teams edge 仍被暂停，并保留 `RECOVERY_REQUIRED`；不会重放 Compose 或 stop。操作员必须先核对确切 Docker 状态；一次性读到旧镜像不能证明超时的 daemon 请求已经结束。

| 函数 | 职责 | 复用的校验 |
| --- | --- | --- |
| `create_plan` | 生成绑定 active/candidate 文件、容器 ID、镜像 ID 与运行态的一次性只读计划。 | 对两组输入调用 `preflight.run_preflight(mode="stage1")`，沿用 owned staging ingress 显式选项；该入口负责主机/容量、Compose、密钥、Provider 范围、入口和 Stage 1 配置核验。`preflight.run_preflight` 通过 `build_arm64.check_local_lock` 核验每份镜像锁。 |
| `_resolve_plan_inputs` | apply/recovery 前重新校验计划输入摘要并渲染 Compose。 | apply 复用完整 Stage 1 preflight 和 `check_local_lock`；recovery 保留配置/镜像锁校验，只跳过动态容量阈值，避免候选资源压力阻止旧镜像恢复。 |
| `_inspect_project_services`, `_verify_live_network_membership` | 回读 Docker project、不可变镜像 ID、健康状态和真实网络 peer。 | 在既有部署检查之上增加 standalone caller 边界；不会启动第二个服务或 scheduler。 |
| `_validate_initial_runtime_state`, `_pause_edge`, `_drain_navigator` | 将新计划绑定 Teams spool CAS 状态，并在替换前排空现有 Navigator executor。 | 通过容器内请求复用 Plugins spool CAS 与 Navigator service-Bearer drain API；停止前要求持久计数完整、active/waiting dispatch 为零、通知轮询暂停且无通知在途。Resume 使用 producer 定义的精确 `{generation, token}` 请求体。 |
| `_update_target`, `_ensure_navigator_resumed` | 使用 `compose up --no-deps --pull never --no-build` 仅替换 `exchange`/`navigator`，再回读镜像 ID、健康状态和 Navigator 接纳状态。 | 沿用计划中已核验的镜像锁引用和上述 runtime inventory helper。 |
| `_rollback_operation`, `resume_operation`, `operation_status` | 回滚前排空仍运行的候选 Nav，只恢复已审核的旧镜像 ID；状态不明时持久记录 `RECOVERY_REQUIRED`。 | 复用相同镜像锁、preflight、容器、网络、drain 和 edge CAS 检查；不会自动重放结果不确定的替换。 |

`runtime_update.py` 只实现这类单服务更新需要的事务顺序与回读；部署配置和 Provider/密钥门禁仍由 preflight 负责，镜像锁核验仍由 `build_arm64.py` 负责。
