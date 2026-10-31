# Teams cloud deployment bundle / Teams 云端部署包

This directory contains the guarded ARM64 build, runtime composition, host preflight,
and bounded cloud-acceptance helpers for the Cyrene Teams workload.

本目录提供 Cyrene Teams 工作负载的受控 ARM64 构建、运行编排、主机预检和有预算上限的云端验收工具。

For unattended publication, Azure CD, safe VM reconciliation and real acceptance,
read [Automatic deployment](AUTOMATIC_DEPLOYMENT.md).

自动发布、Azure CD、安全续跑及真实验收请见[自动部署说明](AUTOMATIC_DEPLOYMENT.md)。

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
| `provider_credential_setup.py` | Loopback-only HF device-OAuth repository picker and prevalidated Runpod API-key setup / 仅 loopback 的 HF 设备 OAuth 仓库选择与验证后写入 Runpod API key |
| `compose.credential-notifications.yaml`, `config/teams-credential-notifications.schema.json`, `config/teams-credential-notifications.template.json`, `config/teams-credential-notifications-exchange-profile.schema.json`, `config/teams-credential-notifications-exchange-profile.template.json` | Optional fixed-owner, fixed-channel credential-health notices using Navigator's existing notification worker / 可选启用由 Navigator 现有通知 worker 投递的固定 owner、固定频道凭据状态提醒 |
| `compose.proactive-assistant-v1.yaml`, `compose.computer-runtime.yaml`, `Dockerfile.computer-runtime`, `Dockerfile.computer-proxy` | Opt-in unlimited conversational model profile, private ephemeral computer runner, and mTLS-only bridge / 可选的不限聊天次数模型配置、内网临时电脑执行容器与仅 mTLS 桥接 |
| `compose.automation-workflows.yaml`, `config/work-actor-token-map.schema.json`, `config/automation-source-bindings.schema.json`, `config/automation-work-principals.patch.{schema,template}.json` | Off-by-default actor-scoped Work tokens, five-person private principal patch, bridge token, and host-selected observation targets / 默认关闭的 actor-scoped Work token、五人私有 principal 合并片段、bridge token 和宿主选择的观察 target |
| `Dockerfile.dsh-executor-codebuddy`, `compose.codebuddy-acp.yaml`, `config/codebuddy-acp.env.template` | CodeBuddy ACP image/profile with a closed command/model selector and dormant pinned Antigravity ACP capability / CodeBuddy ACP 镜像/profile 限定命令和模型选择，并内置固定版本但未启用的 Antigravity ACP 能力 |
| `Dockerfile.dsh-executor-antigravity-acp`, `compose.antigravity-acp.yaml`, `config/antigravity-acp.env.template`, `config/antigravity-acp.env.schema.json` | Opt-in Google Antigravity ACP 1.4.5 image and fixed owner-profile runtime / 可选的 Google Antigravity ACP 1.4.5 镜像与固定 owner profile 运行时 |
| `config/build-plan.antigravity-acp.template.json`, `config/antigravity-acp-third-party-notice.md` | Local-only pinned ARM64 image plan and Google binary provenance / 仅本地构建的固定 ARM64 镜像计划及 Google 二进制来源说明 |
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
| `deployment_probe.py`, `config/deployment-probe*.template.json` | Native Exchange API read probes for HF/Azure/GCP and optional RunPod Pods, with a fixed first page / 通过原生 Exchange API 探测 HF/Azure/GCP 与可选 RunPod Pods，只读第一页 |
| `runtime_update.py` | Owner-confirmed, drain-aware Navigator/Exchange update plan, apply, rollback recovery, and status / 需 owner 明确应用的 Navigator/Exchange 排空热更新计划、回滚恢复与状态查询 |
| `persistence_forward_recovery.py`, [`PERSISTENCE_FORWARD_RECOVERY.md`](PERSISTENCE_FORWARD_RECOVERY.md) | One-shot, exact-image/source migration proof and explicit forward-only recovery for the pinned Persistence schema-gate failure / 针对固定 Persistence schema 门禁故障的一次性精确镜像/源码迁移证明与显式向前恢复 |
| `account_center_core_recovery.py` | Restore only the old Account Center after a proven, unmigrated v8 Graph-bootstrap failure; retain upgraded Persistence and record the mixed runtime / 仅在证明仍为 v8 时恢复旧授权服务，保留新 Persistence 并明确记录混合运行状态 |
| `office_graph_forward_recovery.py` | One-shot forward continuation from the exact restored mixed Graph-bootstrap state; preserve applied Persistence and promote only after the fixed AC migration, full health, and resource readback / 从精确恢复后的混合 Graph 引导状态一次性前向续跑；保留已应用的 Persistence，并在固定 AC 迁移、完整健康与资源回读通过后才提升 selector |
| `host_fronted_canary.py` | Read-only readiness gate for an existing host Caddy front and the private locked ingress / 现有宿主机 Caddy 与锁定私有 ingress 的只读就绪门禁 |
| `plugin_grpc_health_probe.py` | Verify private DirectPluginRuntime mTLS health and plugin identity / 通过内网 mTLS 核验 DirectPluginRuntime 健康状态与插件身份 |
| `acceptance_budget.py` | Legacy CLI resource lifecycle runner for unrelated task IDs; rejects CLI mutation plans for this task / 供其他 task ID 使用的旧版 CLI 生命周期 runner；本任务会拒绝 CLI 资源变更计划 |
| `api_acceptance_budget.py` | Reusable write-ahead USD 5 reservation/state ledger for an existing private DSH API coordinator; never invokes providers / 供既有私有 DSH API 协调器复用的 5 美元写前预留与状态账本；本模块不会调用供应商 |
| `export_authority_keyring.py` | Derive a public-only Ed25519 keyring from private seed files / 从私钥种子导出仅含公钥的 Ed25519 keyring |
| `maintainer_subjects.py` | Run one owner-scoped Directory maintainer change and atomically refresh the Teams actor projection / 执行 owner Directory 增撤并原子刷新 Teams 身份投影 |
| `config/` | Non-secret templates and lock schemas / 非敏感模板与锁文件 Schema |
| [`docs/TEAMS_CLOUD_DEPLOYMENT.md`](../../../docs/TEAMS_CLOUD_DEPLOYMENT.md) | Bilingual staging, identity, provider order, rollback, and evidence runbook / 双语 staging、身份、Provider 顺序、回滚和证据手册 |
| [`AUTOMATION_PILOT_PREPARATION.md`](AUTOMATION_PILOT_PREPARATION.md) | Prepare-only source bindings, actor-scoped Work credentials, provider gates, VM capacity guard, and five-day measurement definitions / 仅准备的 source binding、actor-scoped Work credential、Provider 门禁、VM 容量保护和五日测量定义 |

### Microsoft Graph mail / Microsoft Graph 邮件

The three mail feature switches in `config/microsoft-graph-office-v2-host-profile.template.json` and the provider-settings template default to `false`. Enabling a switch is an explicit host configuration change. An already-linked profile that lacks the selected Mail scopes remains usable for its existing non-mail capabilities; the setup page reports `REAUTHORIZATION_REQUIRED` and waits for a separate user-initiated scope upgrade. Status polling and target discovery never start OAuth automatically.

Mailbox discovery and message reads expose bounded metadata only. Reading message bodies or attachments is not implemented. Draft creation/update/delete and sending are separate opt-in capabilities with their own delegated scopes; sending is limited to an existing draft, never an arbitrary recipient/body payload. Uncertain sends are outcomes of unknown status and must not be replayed automatically.

`config/microsoft-graph-office-v2-host-profile.schema.json` keeps `mailFolderIds` empty unless metadata access is explicitly enabled. `mailMetadataEnabled` controls folder discovery and folder-bound metadata reads; `mailDraftsEnabled` and `mailSendEnabled` use the fixed Drafts folder and do not add arbitrary folder targets.

### Microsoft Graph 邮件

`config/microsoft-graph-office-v2-host-profile.template.json` 和 provider-settings 模板中的三个邮件功能开关默认均为 `false`；启用开关必须是宿主机的显式配置变更。已有链接若缺少所选 Mail scope，仍可继续使用已授权的非邮件能力；设置页会报告 `REAUTHORIZATION_REQUIRED`，并等待用户单独发起 scope upgrade。状态轮询和 target discovery 不会自动启动 OAuth。

邮箱发现和邮件读取仅暴露有界元数据，不支持读取邮件正文或附件。草稿创建/更新/删除和发送是各自独立 opt-in 的能力，并使用对应委托 scope；发送只允许发送已有草稿，不接受任意收件人或正文。发送结果不确定时记录为未知结果，禁止自动重放。

`config/microsoft-graph-office-v2-host-profile.schema.json` 仅在显式启用 metadata 访问后才允许设置 `mailFolderIds`。`mailMetadataEnabled` 控制文件夹发现和绑定文件夹的元数据读取；`mailDraftsEnabled` 与 `mailSendEnabled` 使用固定 Drafts 文件夹，不增加任意文件夹 target。

Run the source build only from a clean, committed Workspace checkout and provide
the exact source SHA for every repository. The builder snapshots remote Git
commits into a temporary directory; it never copies files from sibling working
trees or a user's home directory.

`runtime.env.template` defaults `CYRENE_HF_ENABLED=true`; the HF-enabled path requires `compose.huggingface.yaml`, the real owner-only OAuth seed, current HF binding, and the live HF tool catalog. For GitHub-only bootstrap or Stage 1 staging, load `config/github-only.env.template` after `runtime.env` and omit the HF Compose overlay. Use the GitHub-only Account Center config, `config/private-connector-bootstrap.github-only.template.json`, and an Exchange integration config that omits top-level `authorizationCloud` when the expected provider set is empty. Do not provide `authorizationCloud.profiles: {}`; Exchange rejects an empty `profiles` map when `authorizationCloud` is present. Later stages with expected providers retain the exact non-empty stage-scoped profile map. Preflight requires no HF provider service, source secret, provider secret references, connector binding, target, or catalog entry in GitHub-only mode. HF-disabled mode is limited to bootstrap and Stage 1/staging; later phases require the HF-enabled gates.

`runtime.env.template` 默认将 `CYRENE_HF_ENABLED=true`；启用 HF 时必须显式加入 `compose.huggingface.yaml`，并提供真实 owner-only OAuth seed、当前 HF binding 和实时 HF tool catalog。GitHub-only bootstrap 或 Stage 1 staging 应在 `runtime.env` 后加载 `config/github-only.env.template`，并省略 HF Compose overlay。使用 GitHub-only Account Center 配置和 `config/private-connector-bootstrap.github-only.template.json`；当预期 Provider 集为空时，Exchange 配置必须省略顶层 `authorizationCloud`，不能提供会被 Exchange 拒绝的空 `authorizationCloud.profiles` 映射。后续存在预期 Provider 的阶段继续使用精确的非空阶段 profile 映射。GitHub-only 模式下，预检仍要求没有 HF Provider 服务、source secret、provider secret refs、connector binding、target 或 catalog 项。HF-disabled 仅适用于 bootstrap 和 Stage 1/staging；后续阶段必须先通过 HF-enabled 门槛。

`compose.automation-workflows.yaml` only prepares the existing Navigator/Work boundary and adds no service. Select it separately from the root-owned provider overlay. It defaults workflows and personal chat off and requires private actor-token/source-binding maps plus a separate bridge token. See [`AUTOMATION_PILOT_PREPARATION.md`](AUTOMATION_PILOT_PREPARATION.md) for capacity, source ACL, Work API definition, and personal Bot gates.

`compose.automation-workflows.yaml` 仅准备现有 Navigator/Work 边界，不增加服务；它必须与 root-owned Provider overlay 分开选择。它默认关闭 workflow 与个人聊天，且要求私有 actor-token/source-binding 文件和独立 bridge token。容量、source ACL、Work API definition 与 personal Bot 门禁见 [`AUTOMATION_PILOT_PREPARATION.md`](AUTOMATION_PILOT_PREPARATION.md)。

The GitHub-only cold-start order is: initialize the Directory owner; run `--mode bootstrap` with empty connector bindings/catalog and the GitHub-only bootstrap input; let Exchange produce the real GitHub profile/connection; update the GH-only Account Center allowlist; export the real GitHub and computer bindings/tool catalog; then generate the verified nine-repository workflow allowlist and replace pending target bindings. Bootstrap preflight accepts only the named pending binding/source-workflow markers and returns `BOOTSTRAP_ONLY_NOT_FULL_READY`. Stage 1 requires live bindings, the exact repository allowlist, and matching tool schemas. For CodeBuddy ACP, `CYRENE_WORKFLOWS_ENABLED` defaults to `false`; keep the host WorkFlow candidate disabled through the first owner Teams chat, then read the actual Work API and require all stored v2 definitions to be absent or disabled. The host candidate remains disabled as a preflight manifest while owner-authorized activation is persisted separately through Work API PUT and GET read-back.

GitHub-only 冷启动顺序为：先初始化 Directory owner；使用空的 connector binding/catalog 和 GitHub-only bootstrap 输入运行 `--mode bootstrap`；由 Exchange 返回真实 GitHub profile/connection；更新仅含 GitHub profile 的 Account Center allowlist；导出真实 GitHub 与 computer binding/tool catalog；再生成核实过的九仓库 workflow allowlist 并替换 pending target binding。Bootstrap 预检只接受明确列出的 pending binding/source-workflow 标记，并返回 `BOOTSTRAP_ONLY_NOT_FULL_READY`。Stage 1 要求实时 binding、精确仓库 allowlist 和匹配的 tool schema。CodeBuddy ACP 的 `CYRENE_WORKFLOWS_ENABLED` 默认是 `false`；首次 owner Teams 聊天期间保持 host WorkFlow 候选禁用，再读取实际 Work API，确认所有持久 v2 definition 均不存在或 disabled。host candidate 持续作为 disabled preflight manifest；owner 授权后的启用通过独立 Work API PUT 保存，并用 GET 回读确认。

The Antigravity and CodeBuddy ACP model profiles are optional and mutually exclusive. The CodeBuddy ACP profile uses the base file, `compose.computer-runtime.yaml`, then `compose.codebuddy-acp.yaml`; load `config/codebuddy-acp.env.template` after the private runtime env. This template selects GitHub-only mode until HF authorization is completed, defaults the Workflows engine off, selects only the native `codebuddy` CLI and `glm-5.3-flash` or `deepseek-v4.1-flash`, clears the legacy Harness model source, disables fallback, keeps two bounded foreground/background slots, and uses the service-owned `secrets/codebuddy` login directory. Its WorkFlow `modelPolicy` must use `preferredProvider=codebuddy`, the selected ACP model, and the metadata literal `fallback=configured_no_additional_spend`; the last field does not enable the disabled runtime fallback. Navigator owns the fixed ACP flags and writes each host-MCP bridge configuration to a private 0600 temporary file; Compose must not inject MCP JSON, tokens, or mounts. `CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP=true` is a required owner declaration, not a CodeBuddy CLI billing guarantee. Preflight checks the login directory's owner/mode without reading credentials or contacting CodeBuddy.

The proactive profile is not part of the default stack. Add exactly one model profile after the base Compose file and include the computer-runtime overlay for that deployment. For CodeBuddy ACP, the Workflows engine defaults off; before turning it on, read the durable Work API and verify every stored definition is absent or disabled. The host workflow file is a separate preflight manifest that stays `enabled:false`; after owner acceptance, the authorized Work API write and read-back update the durable definition. Do not activate a schedule before the host target list and owner thread/task IDs are verified. Current-VM capacity and external acceptance remain separate live checks.

Antigravity 与 CodeBuddy ACP 模型 profile 都是可选项，且不能叠加。CodeBuddy ACP 需要按顺序使用基础文件、`compose.computer-runtime.yaml`、`compose.codebuddy-acp.yaml`，并在私有 runtime env 后加载 `config/codebuddy-acp.env.template`。模板默认设置 `CYRENE_WORKFLOWS_ENABLED=false`；owner chat 和实际 Work API 定义 read-back 完成前保持关闭。workflow v2 的 `modelPolicy` 使用 `preferredProvider=codebuddy`、当前 ACP 模型，以及只作为元数据的 `fallback=configured_no_additional_spend`，这不会开启 DSH fallback。它只选择原生 `codebuddy` CLI 和 `glm-5.3-flash` 或 `deepseek-v4.1-flash`，清空旧 Harness model source、关闭运行时 fallback，并沿用服务专属的 `secrets/codebuddy` 登录目录。Navigator 固定 ACP 参数，并为每次调用在私有临时文件中写入权限为 0600 的 host-MCP bridge 配置；Compose 不得注入 MCP JSON、token 或挂载。`CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP=true` 是 owner 必填声明，不是 CodeBuddy CLI 的扣费保证。预检只验证登录目录 owner/权限，不读取凭据，也不联系 CodeBuddy。

The standard CodeBuddy executor image also contains the official Antigravity ACP 1.4.5 executable and its `localharness_external` sibling in a separate hash-verified build stage. Its existing CodeBuddy CLI, `.codebuddy` profile path, default Compose configuration, and active backend selection remain unchanged. Verify the exact locked image without Compose, environment, or provider-auth inputs:

```sh
python3 tooling/deploy/teams-cloud/preflight.py \
  --capability-only antigravity-acp \
  --image-lock "$ARTIFACTS/image-lock.json" \
  --docker-prefix docker
```

This capability check reads the private image lock and Docker image metadata only. It does not select Antigravity, inspect either provider's login state, establish model entitlement, or claim a deployed-runtime change. The normal `teams-cloud-release.yml` path builds the six-image immutable ARM64 bundle on a native ARM64 runner after successful canonical `develop` CI; its image lock, release manifest, publication proof, and remote read-back remain the release evidence. The Google binaries are downloaded only during an explicit image build and are not committed.

CodeBuddy 执行镜像也会在单独的哈希校验构建阶段中安装 Google 官方 Antigravity ACP 1.4.5 主程序及其 `localharness_external` 伴随可执行文件。现有 CodeBuddy CLI、`.codebuddy` profile 路径、默认 Compose 配置和当前 backend 选择保持不变。可在不提供 Compose、环境文件或 Provider 授权的情况下检查确切锁定镜像：

```sh
python3 tooling/deploy/teams-cloud/preflight.py \
  --capability-only antigravity-acp \
  --image-lock "$ARTIFACTS/image-lock.json" \
  --docker-prefix docker
```

此能力检查只读取私有 image lock 和 Docker 镜像元数据；它不会选择 Antigravity、检查任一 Provider 登录状态、证明模型 entitlement，也不代表已更改部署运行时。常规 `teams-cloud-release.yml` 流程会在 canonical `develop` CI 成功后，通过原生 ARM64 runner 构建六镜像不可变发行包；image lock、release manifest、发布证明和远端回读仍是发行证据。Google 二进制仅在显式构建镜像时下载，不会提交到仓库。

The standalone Antigravity ACP profile is an explicit opt-in and uses `compose.yaml`, `compose.computer-runtime.yaml`, then `compose.antigravity-acp.yaml`, with `config/antigravity-acp.env.template` loaded after the private runtime env. The current authenticated owner inventory advertised `gemini-3.8-flash`, so the template recommends that exact ID; Navigator rechecks the session's advertised options and fails closed if it is absent. Leave the model value blank to use the server's native default. The profile build plan is a local one-image packaging check only: it produces a `LOCAL_ONLY` lock containing only `dsh-executor`, so that lock cannot pass full-stack preflight. The normal release image selection remains CodeBuddy ACP. Compose v2.24 or later is required for the profile's `!override` volume replacement, which removes the base CodeBuddy login mount while preserving Navigator session and Teams authorization mounts. Provision `secrets/antigravity-acp` as a distinct UID/GID 10001, mode 0700 directory for the authorized owner's Google OAuth state; never copy a personal Antigravity home. The profile fixes the executable path to `/usr/local/bin/agy_acp_server.par`, installs its `localharness_external` sibling beside it, and clears the legacy Harness model selector. The Workflows engine stays off. Full-stack preflight requires the selected locked Navigator image to carry the fixed Antigravity ACP 1.4.5 profile and archive-hash labels; an ordinary CodeBuddy release image is rejected. Preflight does not read OAuth data, authenticate Google, or establish model entitlement. Navigator configures no provider fallback, but this cannot control Google-side entitlement or billing. Google's proprietary 1.4.5 executables are fetched only during an explicit image build and checked against the architecture SHA-256 pins in [`config/antigravity-acp-third-party-notice.md`](config/antigravity-acp-third-party-notice.md); neither binary is committed here. This source change does not switch a deployed runtime or establish ARM64 acceptance.

独立的 Antigravity ACP profile 需要显式启用：按顺序使用 `compose.yaml`、`compose.computer-runtime.yaml` 与 `compose.antigravity-acp.yaml`，并在私有 runtime env 后加载 `config/antigravity-acp.env.template`。当前已认证 owner 的模型目录公布了 `gemini-3.8-flash`，因此模板推荐这个确切 ID；Navigator 会重新读取 session 公布的选项，若该 ID 不存在则失败关闭。将模型项留空可使用 server 原生默认值。该构建计划只用于本地单镜像打包检查：它生成的 `LOCAL_ONLY` lock 只包含 `dsh-executor`，不能通过整套服务 preflight。常规发行镜像仍使用 CodeBuddy ACP。该 profile 通过 Compose v2.24 及以上版本的 `!override` 替换卷列表，移除基础文件中的 CodeBuddy 登录挂载，同时保留 Navigator 会话和 Teams 授权挂载。将 `secrets/antigravity-acp` 准备为独立目录，UID/GID 为 10001，权限为 0700，用于授权 owner 的 Google OAuth 状态；不要复制个人 Antigravity home。profile 固定 `/usr/local/bin/agy_acp_server.par`，并将 `localharness_external` sidecar 安装在同目录；同时清空旧 Harness model selector。Workflows 引擎默认关闭。整套 preflight 要求当前锁定的 Navigator 镜像携带固定的 Antigravity ACP 1.4.5 profile 与 archive 哈希标签；普通 CodeBuddy 发行镜像会被拒绝。预检不会读取 OAuth 数据、登录 Google 或证明模型 entitlement。Navigator 不配置 Provider fallback，但这无法控制 Google 侧权限或计费。Google 专有 1.4.5 可执行文件只在显式构建镜像时下载，并按 [`config/antigravity-acp-third-party-notice.md`](config/antigravity-acp-third-party-notice.md) 中的架构 SHA-256 固定校验；两个二进制都不会提交到本仓库。本次源码改动不会切换已部署 runtime，也不构成 ARM64 验收。

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

For a VM whose existing host Caddy already fronts the loopback staging ingress,
pass `--ingress-exposure host-fronted-staging` to the connector-auth stage plan.
The default remains `public-cutover`. The explicit mode requires the existing
Compose ingress to stay on `127.0.0.1:18443` with `Caddyfile.staging`, verifies
the exact Docker owner, and rechecks the active host Caddy process/Admin API,
callback route, backend CA/TLS, public TLS, and unsigned callback HTTP 401 during
both plan and apply. This read-only readiness does not apply host Caddy, enable
the public Compose ingress, or claim post-apply callback acceptance; the report
keeps `accepted=false`, `deploymentReady=false`, and
`publicCallbackPostApply=NOT_RUN` until the later live Teams acceptance.

For connector-auth provider bootstrap and activation, the private Exchange
bootstrap may include a `cloud` array only when those exact candidate Cloud
Management profiles are present. Every entry is closed to `profileAlias`,
`targetAlias`, and `expectedAccessMode="workspace_operator"`; the profile alias,
identity, and target must join to the Exchange `authorizationCloud` map and the
Cloud Management phase provider set. Stage 1 must keep `cloud` absent.

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

如果 VM 已由现有宿主机 Caddy 前置到 loopback staging ingress，connector-auth 阶段 plan 必须显式传入 `--ingress-exposure host-fronted-staging`；默认仍为 `public-cutover`。该模式要求 Compose ingress 继续使用 `Caddyfile.staging` 和 `127.0.0.1:18443`，核验精确 Docker owner，并在 plan 与 apply 时重新只读检查宿主机 Caddy 进程/Admin API、回调路由、backend CA/TLS、公网 TLS 和未签名回调 HTTP 401。它不会应用宿主机 Caddy、启用 Compose 公网入口或声称回调已在更新后验收；后续实际 Teams 验收前，报告必须保留 `accepted=false`、`deploymentReady=false` 和 `publicCallbackPostApply=NOT_RUN`。

connector-auth Provider bootstrap/activation 的私有 Exchange bootstrap 只有在候选 Cloud Management profile 确实存在时才可包含 `cloud` 数组。每项严格只有 `profileAlias`、`targetAlias`、`expectedAccessMode="workspace_operator"`；profile alias、identity 和 target 必须与 Exchange `authorizationCloud` 映射及 Cloud Management 阶段 Provider 集合对应。Stage 1 必须省略 `cloud`。

### Phase-preserving image updates / 保留阶段状态的镜像更新

An existing selector-v2 can receive a single Exchange, Hugging Face, Navigator, or Persistence image update
without changing its `provider-bootstrap` or `issued-connector-activation`
configuration phase. Use `runtime_update.py stage-transition plan` with the current
`--phase` and exactly one `--image-update-target exchange`,
`--image-update-target huggingface`, `--image-update-target navigator`, or
`--image-update-target navigator-persistence`. Hugging Face and Persistence are limited to
`issued-connector-activation`. The selector must already exist and name the
same phase. The plan keeps the service inventory, config and secret directories,
config/secret manifests, HF seed, RunPod key, roles, and every service mount
identity unchanged; only the selected image reference/ID and its build-lock row
may change. External image locks and every non-target build row remain exact.

Apply reuses the V2 ingress pause, Navigator drain, online Work SQLite snapshot,
target health checks, full service/mount inventory, capacity gate, and atomic
selector promotion. An Exchange-only update verifies live Exchange readiness
after the candidate is healthy, then resumes the same Navigator process
generation. During provider bootstrap, readiness compares the full live binding
and tool-catalog projections to the frozen current config, without requiring
future HF/cloud profiles to be issued. During issued-connector activation, it
also verifies each configured provider's live profile/connection scope. A
Hugging Face image update checks the live Exchange readiness for all 14 bindings
and the native catalog before and after replacement. Immediately before Compose,
it joins the selected HF connection from that live snapshot to the frozen binding,
then reads the authorization monitor state and connector invocation ledger.
Replacement requires no active HF monitor lease, no nonterminal HF call, and
`next_check_at` later than the current time by the configured health timeout plus
60 seconds. The monitor keeps its normal schedule; a missing or short window stops
the transition before the HF container changes. The original Navigator process
resumes after the post-update Exchange check. A
Persistence image update verifies Exchange readiness before replacement, then
requires zero unexpired Session writer leases and an exact schema/row-digest
match against a fresh post-drain read immediately before Compose runs. Its
post-start gate requires the same schema and every table row digest to remain
unchanged; only then may the original Navigator process and ingress resume.
Any possible Persistence replacement makes later failure `RECOVERY_REQUIRED`;
the updater never restores the old Persistence image or SQLite snapshot bytes.
A Navigator image update still requires a healthy fresh process instance. The V1
`runtime_update.py plan --target exchange` command is for the Stage 1 inventory
and does not update selector-v2; do not use it for an active 14-service
selector-v2 deployment.

### In-place memory increases / 原位增加内存上限

For an existing issued-activation selector, `stage_transition.py plan
--memory-update-target <service>` plans one strict memory increase on an
already-running service. Active and candidate Compose definitions must be
identical except for that service's `mem_limit`, `memswap_limit`, and a matching
`deploy.resources.limits.memory` when present. Images, locks, environment and
secret files, mounts, CPU, PIDs, ports, security settings, and the exact
14-service inventory remain fixed. Both memory limits must be finite and equal,
preserving the no-swap policy. The plan checks actual Docker memory and CPU
limits for all 14 pinned containers, a total memory cap of 3584 MiB, a CPU cap
of 1.95, at least 1 GiB `MemAvailable`, and at least 2 GiB free disk.

Apply records intent, reads live Exchange readiness, and calls Docker's
single-container `update --memory N --memory-swap N <container-id>` API. It
never runs Compose `up`, recreates or pauses the target, pauses Teams ingress,
drains Navigator, or snapshots/restores SQLite. It verifies the same container
ID and image, target health, every other HostConfig field, all 14 actual
resource caps, live Exchange readiness, and unchanged ingress/Navigator state
before promoting the existing selector. Docker's immutable
`com.docker.compose.config-hash` label remains at its prior hash after an
in-place update; the plan records active, candidate, and live hashes. A later
plan accepts that stale label only when the existing selector-linked journal
chain proves the same container received the preceding memory update. An
uncertain or partial update becomes `RECOVERY_REQUIRED`; the tool never lowers
the limit automatically.

For a deployed 14-service `provider-bootstrap` selector whose multi-network
Hugging Face or Cloud Management listener is bound to one Docker address,
`stage-transition plan --listener-repair` provides a closed command-only repair.
It requires the current selector-v2, the exact 14-service inventory, unchanged
images, environment/config/secret files, provider seed, roles, and mount
identities; only the fixed HF and Cloud `--listen 0.0.0.0:7443` command arrays
may change. The plan and apply both verify the disabled Exchange runner and
idle durable refresh work, pause ingress, drain the same Navigator generation,
and snapshot Work SQLite. Apply updates HF and Cloud with no dependencies,
checks both exact command and environment readbacks, then requires the pinned
Exchange's live readiness command to return and validate all 14 bindings and
the native tool catalog before selector promotion and ingress resume. The
metadata-only precheck does not call provider `ListTools` and is not live
catalog evidence. Do not use this repair during issued activation or with an
image/configuration update.

现有 selector-v2 可在保留 `provider-bootstrap` 或
`issued-connector-activation` 配置阶段的同时，只更新一个 Exchange、HF、Navigator
或 Persistence 镜像。使用 `runtime_update.py stage-transition plan`，传入 selector
当前的 `--phase`，并且只指定一个 `--image-update-target exchange`、
`--image-update-target huggingface`、`--image-update-target navigator` 或
`--image-update-target navigator-persistence`。HF 与 Persistence 只允许用于
`issued-connector-activation`。必须已有 selector-v2 且其 phase 与参数一致。
计划固定服务集合、配置和 secrets 目录、配置/secret 清单、HF seed、RunPod key、
角色以及每个服务的 mount identity；只允许目标镜像引用/ID 及对应 build lock 行
变化。外部镜像锁和所有非目标 build 行必须保持完全一致。

Apply 复用 V2 ingress pause、Navigator drain、在线 Work SQLite snapshot、目标健康
检查、完整服务/mount 清单、容量门和原子 selector promotion。Exchange-only 更新
会在候选 Exchange 健康后做 live readiness 检查，再恢复同一 Navigator process
generation。Provider bootstrap 阶段将当前完整 live binding 和 tool catalog 与冻结
配置逐项比较，不要求尚未签发的 HF/cloud profile；issued-connector-activation
阶段还会核对各已配置 Provider 的 live profile/connection scope。HF 镜像更新会在替换前后
核验 Exchange 的全部 14 个 binding 和原生 catalog。替换前紧邻 Compose 更新时，会从 live
Exchange snapshot 取出 HF connection，并与冻结 binding join，再只读检查对应的授权监视器
和 connector invocation 状态。必须没有活动 HF lease、没有未终态 HF 调用，且
`next_check_at` 晚于当前时间至少 `health timeout + 60s`。监视器保持正常调度；缺少或过短的
检查窗口会在 HF 容器变更前阻止操作。完成替换后的 Exchange 核验后，恢复原 Navigator process。
Persistence 镜像更新
会先核验 Exchange readiness，然后要求所有 Session writer lease 均已过期，并在紧贴
Compose 更新前读取一次数据库，确认 schema 和全部行摘要与 drain 后快照完全一致。
更新后还会再次确认 schema 与所有行摘要不变，之后才恢复原 Navigator process 和 ingress。
Persistence 替换可能开始后若再失败，状态进入 `RECOVERY_REQUIRED`；更新器不会自动回滚
旧 Persistence 镜像，也不会用 SQLite 快照字节覆盖数据库。Navigator 镜像更新仍要求健康的新 process instance。V1 `runtime_update.py plan --target exchange` 面向
Stage 1 inventory，且不会推进 selector-v2；已有 14-service selector-v2 部署不可用该 V1 命令。

对于已部署的 14-service `provider-bootstrap` selector，如果多网卡 Hugging Face
或 Cloud Management 监听绑定到单个 Docker 地址，可用
`stage-transition plan --listener-repair` 执行封闭的 command-only 修复。它要求当前
selector-v2、精确 14-service inventory，以及镜像、环境/配置/secret 文件、Provider
seed、角色和 mount identity 全部不变；只允许 HF 与 Cloud 的固定
`--listen 0.0.0.0:7443` command 数组变化。plan/apply 都核验 Exchange runner 已关闭且
持久 refresh 工作空闲，暂停 ingress，drain 同一 Navigator generation，并在线备份
Work SQLite。Apply 以 no-deps 更新 HF 与 Cloud，核验两个目标容器的精确 command 与
环境读回，然后要求 pinned Exchange 的 live readiness 命令返回并校验全部 14 个
binding 和原生 tool catalog，之后才提升 selector 并恢复 ingress。修复前的 metadata-only
检查不会调用 Provider `ListTools`，不能视为 live catalog 证据。issued activation 阶段
或任何镜像/配置更新不得使用此修复。

### 原位增加内存上限

对于已有的 issued-connector-activation selector，可用
`stage_transition.py plan --memory-update-target <service>` 为一个正在运行的服务
规划严格的内存上限增加。Active 与 candidate Compose 除目标服务的
`mem_limit`、`memswap_limit` 和（若存在）匹配的
`deploy.resources.limits.memory` 外必须完全一致。镜像、锁、环境与 secret 文件、挂载、
CPU、PID、端口、安全设置及精确 14 服务 inventory 均保持不变。两个内存值必须有限且相等，
以保留无 swap 策略。计划检查 14 个固定容器的实际 Docker 内存与 CPU 限制、总内存不超过
3584 MiB、CPU 不超过 1.95、`MemAvailable` 至少 1 GiB，以及至少 2 GiB 可用磁盘。

Apply 会先记录更新意图并读取实时 Exchange readiness，然后仅对固定容器 ID 调用 Docker
单容器 `update --memory N --memory-swap N` API。不会运行 Compose `up`，不会重建或暂停目标，
不会暂停 Teams ingress、drain Navigator，也不会快照或恢复 SQLite。随后核验同一容器 ID 和镜像、
目标健康状态、其余所有 HostConfig 字段、14 个容器的实际资源上限、实时 Exchange readiness，
以及 ingress 和 Navigator 状态未变化，再提升现有 selector。Docker 不可变的
`com.docker.compose.config-hash` 标签在原位更新后仍保留旧 hash；计划会记录 active、candidate
和 live hash。后续计划只在现有 selector 关联的 journal 链证明同一容器已执行前一次内存更新时，
才接受该旧标签。不确定或部分更新会进入 `RECOVERY_REQUIRED`；工具不会自动降低内存上限。

### Private provider credential setup / 私有 Provider 凭据配置

The operator helper is a local-only page, not a credential service. Run it as UID
10001 on the deployment host, using the private artifact and secret directories
already provisioned for that task. It binds only `127.0.0.1`; on a remote VM,
reach it through an SSH local forward and open the exact `127.0.0.1` URL printed
by the process. Do not put the page behind public Caddy or another public route.

```bash
sudo -u '#10001' -- python3 /absolute/path/to/Cyrene-Workspace/tooling/deploy/teams-cloud/provider_credential_setup.py \
  --artifacts-dir /absolute/path/to/Cyrene/.artifacts/teams-cloud-task \
  --secrets-dir /absolute/path/to/Cyrene/.artifacts/teams-cloud-task/secrets \
  --active-env-file /absolute/path/to/Cyrene/.artifacts/teams-cloud-task/config/runtime.env \
  --hf-seed /absolute/path/to/Cyrene/.artifacts/teams-cloud-task/onboarding/private/huggingface-oauth-bootstrap.json \
  --port 51876
```

`--secrets-dir` is validated read-only and is never modified by the page. Both
HF and Runpod writes go to new versioned directories beneath
`onboarding/private/provider-candidates/`; each save returns the exact
candidate `runtime.env`, empty `config/` directory, and provider file paths.
The private operator preparation must populate that candidate config directory
before a plan can pass preflight. Do not copy a candidate over the active env
or active secrets. Use those returned paths in the reviewed stage-transition
plan.

If the helper runs on a remote VM, create a tunnel from the operator computer:

```bash
ssh -N -L 51876:127.0.0.1:51876 <VM-SSH-ALIAS>
```

Then browse to `http://127.0.0.1:51876/`. The page requires an exact loopback
`Host`, matching `Origin`, a per-session CSRF token, and a SameSite/HttpOnly
cookie. The HF flow uses the official device authorization endpoints. An
existing owner-only seed can be loaded for repository selection without logging
in again; a new device login starts only when the operator clicks the button.
The server keeps access and refresh tokens in process memory, calls the Hub
identity/repository APIs itself, and lists model, dataset, and Space namespaces
without requiring a repository URL to be pasted. After the operator selects
1–100 listed repositories, it writes a versioned owner-only candidate bundle
containing a closed OAuth seed and a complete `runtime.env` candidate. The
candidate synchronizes repository scope across the seed, the HF provider
configuration, and the `hf-primary` Exchange profile. The active env file is
read-only and is never overwritten. The page returns both candidate paths and
the env-file SHA-256 for the formal stage-transition plan. No token, device
code, request body, or provider response body is rendered or logged.

Saving the candidate does not by itself update Account Center's durable profile
or activate the new repository scope. The formal stage-transition plan must
consume the exact candidate env and seed, and the verified owner must complete
the existing Exchange profile-link/rotate action before the connector is
considered active. Keep the seed owner-only for the configured recovery path;
profile status and expiry are checked through Exchange's
AuthorizationCloudRunner, which remains the only health poller.

For Runpod, create a Restricted API key and enable only the Pod operations
approved for the profile. The helper sends that candidate only to Runpod's
read-only `GET /v2/pods?limit=1` endpoint, then atomically publishes a
versioned private bundle containing `runtime.env`, an empty `config/` target,
and `runpod-api-key` (UID 10001, mode `0400`) after HTTP 200. The candidate env
sets `CYRENE_CONFIG_DIR` to the returned config target and overrides only
`CYRENE_RUNPOD_API_KEY_FILE`; the active secrets directory and active key stay
unchanged. A 401, 403, rate limit, malformed response, or network failure
publishes nothing. This probe confirms Pod-list access only; it does not prove
write authorization. Root-private configuration preparation must populate the
candidate config directory before the formal plan. Cloud Management changes
only after that reviewed apply recreates the locked service and reads back
health.

The credential notice overlay is optional and must remain absent until the
Directory owner, Teams tenant/team/channel/thread/service URL, Exchange
system-notification profile, and every provider connection ID come from verified
private configuration. The Exchange profile is a fixed subprofile at
`connectorHost.teamsMessages.systemNotification`; its `ownerSubject` and
`fixedRoute` must exactly match the Navigator notification config, while its
connection and service identity inherit from the existing Teams parent profile.
Keep both templates' unregistered IDs empty and do not load the overlay with
either template. The Navigator principal template keeps
`can_publish_credential_health=false`; enable the role only after the fixed
owner and destination have been verified. Notices reuse Navigator's existing
system worker and outbox; this does not enable a general model-driven proactive
workflow.

HF 的 profile 仍由唯一的 Exchange AuthorizationCloudRunner 查询健康状态；Workspace 不会再创建第二个轮询器。`compose.credential-notifications.yaml` 是独立可选叠加层，只能在 Directory owner、Teams tenant/team/channel/thread/service URL、Exchange `connectorHost.teamsMessages.systemNotification` profile 和每个 provider connection ID 都来自真实私有配置后加载。Exchange 子profile中的 `ownerSubject` 与 `fixedRoute` 必须和 Navigator 通知配置逐字段一致，连接和服务身份继承既有 Teams 父profile。两个模板中的 ID 保持空值时不得加载 overlay。Navigator principal 模板默认 `can_publish_credential_health=false`；只有固定 owner/destination 完成核验后，才在私有 principal 配置中显式启用该角色。通知使用 Navigator 现有 system worker 和 outbox，不开启通用的模型主动工作流。

### Runtime updater function index / 热更新函数索引

| Function | Responsibility | Shared validation used |
| --- | --- | --- |
| `create_plan` | Write a read-only plan bound to active/candidate files, running container IDs, image IDs, and service state. | Calls `preflight.run_preflight` for both input sets in `stage1` mode with the existing owned-staging-ingress flag; that path validates host/capacity, Compose, secrets, provider scope, ingress, and Stage 1 config. `preflight.run_preflight` calls `build_arm64.check_local_lock` for each lock. |
| `_resolve_plan_inputs` | Re-hash and render the reviewed inputs immediately before apply or recovery. | Apply reuses full Stage 1 preflight and `check_local_lock`; recovery reuses their configuration/image-lock checks while skipping only dynamic capacity thresholds, so a failed candidate cannot prevent restoration of the old image. |
| `_inspect_project_services`, `_verify_live_network_membership` | Read Docker project inventory, immutable IDs, health, and actual network peers. | Extends existing deployment checks with the updater's standalone-caller contract; it does not start a second service or scheduler. |
| `_validate_initial_runtime_state`, `_pause_edge`, `_drain_navigator` | Bind the fresh plan to Teams spool CAS state and drain the existing Navigator executor before replacement. | Uses Plugins spool CAS and Navigator's service-Bearer drain API; it requires complete durable counts, zero active/waiting dispatches, and a paused, idle notification worker before stop. Resume sends the producer's exact `{generation, token}` body. |
| `_update_target`, `_ensure_navigator_resumed` | Replace only `github-issues`, `exchange`, or `navigator` with `compose up --no-deps --pull never --no-build`, then read back image ID, health, and Navigator acceptance. The canonical order is GitHub provider, Exchange, then Navigator. | Reuses the plan's verified image-lock references and the runtime inventory helpers above. |
| `_rollback_operation`, `resume_operation`, `operation_status` | Drain a running candidate before rollback, restore only reviewed old image IDs, or retain a `RECOVERY_REQUIRED` journal when state is unknown. | Uses the same lock, preflight, container, network, drain, and edge-CAS checks; recovery never repeats an uncertain replacement automatically. |

`runtime_update.py` adds only the transaction sequencing and read-back checks needed for these approved service updates. Preflight remains authoritative for deployment configuration and provider/secret gates; image-lock verification remains in `build_arm64.py`.

Runtime update commands run as root. They share one root-owned mode-0700 lock directory at `/run/cyrene-runtime-update`, with root-owned mode-0600 project lock files, independent of the selected journal directory. Rootless plan/apply/status/resume is rejected so a second host UID cannot bypass the single-writer lock. The CLI updates only the GitHub Issues provider (`github-issues` Compose service / `github-issues-provider` image-lock row), `exchange`, and `navigator`; it never replaces the Teams ingress or Account Center. Each plan/apply transaction pauses durable ingress delivery and drains Navigator; a provider-only update uses `--no-deps`, verifies the exact image ID and gRPC health, then resumes the same Navigator generation and ingress.

When one candidate lock changes both the GitHub Issues provider and DSH/Navigator rows, one reviewed plan may select `--target github-issues --target navigator`. The operation keeps ingress paused and Navigator drained while it updates the provider first and Navigator second, then releases traffic only after both locked images and health checks pass. Use a provider-only plan with `--target github-issues` when the Navigator image is already active.

Existing schema-v1 Nav/Exchange plans remain valid for `status`/`resume`; provider-target plans use the expanded target policy and are never accepted under the legacy policy.

Use `plan` to bind the active deployment and candidate files, inspect the output plan and its SHA-256, then explicitly run `apply` with that digest. `status` reads the private journal. `resume` performs deterministic rollback only when no Docker mutation has an unresolved outcome. If Docker timed out or the process stopped after a persisted replacement/stop intent, `resume` reconfirms the Teams edge hold, leaves the operation in `RECOVERY_REQUIRED`, and does not repeat Compose or stop. An operator must first reconcile the exact Docker state; a transient read of the old image is not proof that a timed-out daemon request has ended.

所有 runtime update 命令必须由 root 执行。它们共用 `/run/cyrene-runtime-update` 下 root 所有、权限 0700 的锁目录，以及 root 所有、权限 0600 的项目锁文件；锁与选择的 journal 目录无关。为避免其他主机 UID 绕过单写锁，CLI 会拒绝 rootless plan/apply/status/resume。CLI 仅更新 GitHub Issues Provider（Compose 服务 `github-issues`／image-lock 行 `github-issues-provider`）、`exchange` 和 `navigator`；不会替换 Teams ingress 或 Account Center。每次 plan/apply 都会暂停持久投递并排空 Navigator；仅 Provider 的更新使用 `--no-deps`，回读精确镜像 ID 与 gRPC health 后，再恢复同一 Navigator generation 和 ingress。

如果同一 candidate lock 同时更新 GitHub Issues Provider 与 DSH/Navigator，可在一个审核计划中选择 `--target github-issues --target navigator`。操作会保持 ingress 暂停和 Navigator 排空，先更新 Provider、再更新 Navigator；只有两者都通过锁定镜像和健康回读后才恢复流量。Navigator 镜像已就绪时，可单独用 `--target github-issues` 更新 Provider。

既有 schema-v1 Nav/Exchange 计划继续可用于 `status`/`resume`；Provider target 计划必须使用扩展后的 target policy，不能套用旧版 policy。

先用 `plan` 绑定当前部署和候选文件，检查输出计划及其 SHA-256，再用该摘要明确执行 `apply`。`status` 读取私有 journal。只有不存在未决 Docker 操作时，`resume` 才会执行确定性回滚。如果 Docker 超时，或进程在已持久化替换/停止意图后退出，`resume` 会重新确认 Teams edge 仍被暂停，并保留 `RECOVERY_REQUIRED`；不会重放 Compose 或 stop。操作员必须先核对确切 Docker 状态；一次性读到旧镜像不能证明超时的 daemon 请求已经结束。

| 函数 | 职责 | 复用的校验 |
| --- | --- | --- |
| `create_plan` | 生成绑定 active/candidate 文件、容器 ID、镜像 ID 与运行态的一次性只读计划。 | 对两组输入调用 `preflight.run_preflight(mode="stage1")`，沿用 owned staging ingress 显式选项；该入口负责主机/容量、Compose、密钥、Provider 范围、入口和 Stage 1 配置核验。`preflight.run_preflight` 通过 `build_arm64.check_local_lock` 核验每份镜像锁。 |
| `_resolve_plan_inputs` | apply/recovery 前重新校验计划输入摘要并渲染 Compose。 | apply 复用完整 Stage 1 preflight 和 `check_local_lock`；recovery 保留配置/镜像锁校验，只跳过动态容量阈值，避免候选资源压力阻止旧镜像恢复。 |
| `_inspect_project_services`, `_verify_live_network_membership` | 回读 Docker project、不可变镜像 ID、健康状态和真实网络 peer。 | 在既有部署检查之上增加 standalone caller 边界；不会启动第二个服务或 scheduler。 |
| `_validate_initial_runtime_state`, `_pause_edge`, `_drain_navigator` | 将新计划绑定 Teams spool CAS 状态，并在替换前排空现有 Navigator executor。 | 通过容器内请求复用 Plugins spool CAS 与 Navigator service-Bearer drain API；停止前要求持久计数完整、active/waiting dispatch 为零、通知轮询暂停且无通知在途。Resume 使用 producer 定义的精确 `{generation, token}` 请求体。 |
| `_update_target`, `_ensure_navigator_resumed` | 使用 `compose up --no-deps --pull never --no-build` 仅替换 `github-issues`/`exchange`/`navigator`，再回读镜像 ID、健康状态和 Navigator 接纳状态；顺序固定为 Provider、Exchange、Navigator。 | 沿用计划中已核验的镜像锁引用和上述 runtime inventory helper。 |
| `_rollback_operation`, `resume_operation`, `operation_status` | 回滚前排空仍运行的候选 Nav，只恢复已审核的旧镜像 ID；状态不明时持久记录 `RECOVERY_REQUIRED`。 | 复用相同镜像锁、preflight、容器、网络、drain 和 edge CAS 检查；不会自动重放结果不确定的替换。 |

`runtime_update.py` 只实现这三类已批准服务更新需要的事务顺序与回读；部署配置和 Provider/密钥门禁仍由 preflight 负责，镜像锁核验仍由 `build_arm64.py` 负责。
