# Teams cloud deployment / Teams 云端部署

This runbook describes the ARM64 deployment bundle in [`tooling/deploy/teams-cloud/`](../tooling/deploy/teams-cloud/README.md). It keeps Navigator as the sole task executor, Exchange as the connector and reply boundary, Directory as the maintainer authority, and the Teams ingress sidecar as a narrow public callback adapter.

本手册说明 [`tooling/deploy/teams-cloud/`](../tooling/deploy/teams-cloud/README.md) 中的 ARM64 部署包。Navigator 是唯一任务执行器；Exchange 负责连接器和回复边界；Directory 是维护者授权依据；Teams ingress sidecar 只承接受限的公网回调。

## Evidence boundary / 证据边界

The bundle provides build, transfer, configuration, read-only preflight, and bounded resource-cleanup code. It does not prove that the current VM has enough disk or memory for the built images, that Teams DNS/callback has been switched, or that GitHub, Hugging Face, Azure, GCP, or RunPod operations passed. Treat those checks as `NOT_RUN` until a fresh receipt records the exact source/image IDs, host facts, and provider read-backs. The previous Docker stack must stay available until staging, Teams thread continuation, connector checks, and rollback have passed.

本部署包提供构建、镜像传输、配置、只读预检，以及受预算限制的资源回收代码。它不能证明当前云机有足够磁盘或内存承载构建镜像，不能证明 Teams DNS/回调已切换，也不能证明 GitHub、Hugging Face、Azure、GCP 或 RunPod 操作验收通过。除非新证据记录了精确源码和镜像 ID、主机容量及供应商回读，否则这些验收均为 `NOT_RUN`。在 staging、Teams 原帖续问、连接器检查和回滚通过前，旧 Docker 栈必须保持可恢复。

The production gate is native Linux ARM64, at least 2 CPUs and the nominal 4 GiB RAM class (at least 3584 MiB guest-visible `MemTotal` after firmware/kernel reservations), at least 2 GiB root-free reserve after transfer, and an online Tailscale daemon. The proactive profile adds the computer runner and its mTLS proxy: Stage 1 caps persistent services at 3,008 MiB and 1.80 CPUs; Stage 2 and Stage 3 cap them at 3,392 MiB and 1.90 CPUs. One-shot Directory/bootstrap commands are excluded and run separately. Preflight reads current `MemAvailable` and requires at least 1,024 MiB before staging or cutover. If the full cgroup budget plus reserve does not fit yet, staging is allowed only to collect real per-service RSS; cutover then requires healthy staged services, RSS read-back, and the same live memory reserve. The transfer helper measures each image's conservative materialized disk footprint, including rootfs and compressed layers, and requires `1.10 × image footprints + runtime reserve` free before loading. Compressed archive size and a historical free-space report are not substitutes for that measured gate. The added budget is a configured ceiling, not a claim that the current VM can support it; read the live host before staging and do not resize automatically.

生产门槛为原生 Linux ARM64、至少 2 CPU 和 4 GiB RAM 规格（扣除固件和内核保留内存后，guest 可见 `MemTotal` 至少 3584 MiB）、镜像传输后根盘仍至少空闲 2 GiB，并且现有 Tailscale daemon 在线。主动助手 profile 增加电脑执行容器和 mTLS 代理：Stage 1 常驻服务 cgroup 总上限为 3,008 MiB 和 1.80 CPU；Stage 2/3 为 3,392 MiB 和 1.90 CPU。Directory/bootstrap 一次性任务不计入常驻总量，须单独运行。预检读取实时 `MemAvailable`，staging 和切换前都至少保留 1,024 MiB。如果完整 cgroup 上限加预留空间暂时放不下，仍可进行 staging 来实测每个服务 RSS；切换预检还会要求服务健康、RSS 回读完整且实时内存预留达标。镜像传输脚本会实测包含 rootfs 和压缩层的保守落盘占用，并要求传输前可用空间达到“镜像占用总和 × 1.10 + 运行时预留”。压缩包大小和历史空闲空间读数均不能替代该测量门槛。这些是配置上限，不代表已核实当前 VM 能承载；staging 前必须读取实时主机容量，不自动扩容。

## Private inputs and identities / 私有配置与身份

Copy `config/runtime.env.template`, `config/secret-files.template.json`, `config/navigator-principal-config.template.json`, `config/teams-actor-subjects.template.json`, `config/teams-connector.template.json`, `config/connector-bindings.schema.json`, `config/connector-bindings.bootstrap.schema.json`, `config/connector-bindings.empty.template.json`, `config/connector-tool-catalog.schema.json`, `config/connector-tool-catalog.bootstrap.schema.json`, `config/connector-tool-catalog.empty.template.json`, the bootstrap template for the selected provider mode (`config/private-connector-bootstrap.github-only.template.json` or `config/private-connector-bootstrap.template.json`), `config/cloud-api-evidence.schema.json`, both `config/cloud-api-evidence.*.template.json` files, and `config/build-plan.template.json` to a task directory under the umbrella `Cyrene/.artifacts/teams-cloud-<task>/`. The deployment config and receipt directories must remain outside every source worktree. Replace each template only with verified non-secret values; keep credential material in separate owner-only files under the matching private `secrets/` directory.

将 `config/runtime.env.template`、`config/secret-files.template.json`、`config/navigator-principal-config.template.json`、`config/teams-actor-subjects.template.json`、`config/teams-connector.template.json`、`config/connector-bindings.schema.json`、`config/connector-bindings.bootstrap.schema.json`、`config/connector-bindings.empty.template.json`、`config/connector-tool-catalog.schema.json`、`config/connector-tool-catalog.bootstrap.schema.json`、`config/connector-tool-catalog.empty.template.json`、所选 Provider 模式对应的 bootstrap 模板（`config/private-connector-bootstrap.github-only.template.json` 或 `config/private-connector-bootstrap.template.json`）、`config/cloud-api-evidence.schema.json`、两个 `config/cloud-api-evidence.*.template.json` 文件和 `config/build-plan.template.json` 复制到 umbrella 的 `Cyrene/.artifacts/teams-cloud-<task>/` 任务目录。部署配置和回执目录必须位于所有源码工作树之外。模板只填入已核实的非敏感值；凭据分别放在对应私有 `secrets/` 目录中，并设置为仅所有者可读。

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

## GitHub-only bootstrap and Stage 1 / GitHub-only bootstrap 与 Stage 1

The default runtime template selects `CYRENE_HF_ENABLED=true`; this requires the separate `compose.huggingface.yaml` overlay and real HF OAuth seed, provider credentials, binding, and tool catalog. To stage GitHub first, create `$ARTIFACTS/github-only.env` from `config/github-only.env.template` and set `CYRENE_HF_ENABLED=false`. For CodeBuddy ACP, also copy `config/codebuddy-acp.env.template` to `$ARTIFACTS/codebuddy-acp.env`; it selects the same GitHub-only mode, the two bounded foreground/background slots, and `CYRENE_WORKFLOWS_ENABLED=false`. Load the GitHub-only env file last and do not include `compose.huggingface.yaml`.

Use the prepared GitHub-only Account Center JSON, an Exchange integration config that omits the top-level `authorizationCloud` property while no cloud provider profiles are expected, and a `connectorHost.computerRunner` entry matching the selected computer-runtime overlay. Do not provide an empty `authorizationCloud.profiles` map: Exchange accepts the whole property as optional but requires its `profiles` map to be non-empty when present. For later phases that expect providers, include exactly the stage-scoped non-empty profile map. Also use `config/private-connector-bootstrap.github-only.template.json`. Populate its actor from the same Directory owner, issuer, organization, and Workspace as the Teams admission projection; preflight rejects a different in-scope operator before Exchange can link a profile. Exchange's bootstrap schema makes the `huggingface` entry optional, and Account Center accepts an empty provider-secret-file-ref list. The `secret-files.template.json` divides the base Phase 1 credentials from HF-only credentials. In GitHub-only mode, Compose has no HF provider, OAuth source mount, HF-only HMAC/mTLS secret, or HF network; preflight requires the Account Center configuration, Exchange providers, connector bindings, targets, and tool catalog to contain no HF entry. The ACP computer connector may remain enabled and is checked separately; its Exchange binding and live tools are included in the exported Stage 1 map/catalog.

GitHub-only 模式使用预备好的 Account Center 配置；当当前阶段不需要云 Provider profile 时，Exchange 集成配置必须省略顶层 `authorizationCloud`。不要提供空的 `authorizationCloud.profiles` 映射：Exchange 将整个属性设为可选，但一旦存在，就要求其 `profiles` 非空。后续阶段需要 Provider 时，必须提供与阶段严格对应的非空 profile 映射。同时使用 `config/private-connector-bootstrap.github-only.template.json`，并保证 `connectorHost.computerRunner` 与选定的 computer-runtime overlay 一致。actor 必须来自与 Teams admission projection 相同的 Directory owner、issuer、organization 和 Workspace；如果使用同租户范围内的其他 operator，预检会在 Exchange 绑定 profile 前拒绝。Exchange bootstrap schema 允许省略 `huggingface` 项，Account Center 也接受空的 provider-secret-file-ref 列表。`secret-files.template.json` 将 Phase 1 基础凭据与 HF 专属凭据分开。GitHub-only Compose 不包含 HF Provider、OAuth source 挂载、HF HMAC/mTLS secret 或 HF 网络；预检会确认 Account Center 配置、Exchange Provider、connector binding、target 和 tool catalog 均不含 HF 项。ACP computer connector 可单独启用；其 Exchange binding 和实时工具会包含在 Stage 1 导出映射/目录中。

The bootstrap preflight accepts the exact `UNBOUND_PENDING_EXCHANGE_GITHUB_REBIND` target marker and `REPLACE_WITH_APPROVED_SOURCE_WORKFLOW_ID` source-workflow marker only in `bootstrap` mode. It remains `BOOTSTRAP_ONLY_NOT_FULL_READY`. The private workflow file may be absent or remain disabled with pending owner thread/task IDs. Bootstrap preflight does not require the live GitHub workflow inventory or full responsibility allowlist; Stage 1 does.

For CodeBuddy ACP, copy the disabled workflow template to `$ARTIFACTS/proactive-assistant-workflow.v2.json` and set its `modelPolicy` to the configured native ACP profile: `{"preferredProvider":"codebuddy","preferredModel":"glm-5.3-flash","fallback":"configured_no_additional_spend"}` (use the selected `deepseek-v4.1-flash` value when that model is selected). This three-field value is WorkFlow metadata only; ACP fallback remains disabled in Compose and does not turn on the DSH model selector. Stage 1 preflight requires the metadata to match the selected CodeBuddy model.

CodeBuddy ACP 路径将禁用 workflow 模板复制为 `$ARTIFACTS/proactive-assistant-workflow.v2.json`，并把 `modelPolicy` 设置为所选原生 ACP 配置：`{"preferredProvider":"codebuddy","preferredModel":"glm-5.3-flash","fallback":"configured_no_additional_spend"}`（选择 `deepseek-v4.1-flash` 时将模型值改为该项）。这三个字段只是 WorkFlow 元数据；Compose 中 ACP fallback 仍关闭，也不会启用 DSH model selector。Stage 1 预检会要求其与实际 CodeBuddy 模型一致。

For the ACP path, run the same profile selection for bootstrap and Stage 1. The Compose and env-file arguments shown here are the complete selection used for both checks and service startup:

ACP 路径的 bootstrap 与 Stage 1 使用相同 profile。下面的 Compose 文件和 env 文件参数用于预检与服务启动，保持一致：

```sh
sudo -n install -o 10001 -g 10001 -m 0600 \
  tooling/deploy/teams-cloud/config/github-only.env.template \
  "$ARTIFACTS/github-only.env"
sudo -n install -o 10001 -g 10001 -m 0600 \
  tooling/deploy/teams-cloud/config/codebuddy-acp.env.template \
  "$ARTIFACTS/codebuddy-acp.env"
sudo -n install -o 10001 -g 10001 -m 0600 \
  tooling/deploy/teams-cloud/config/proactive-assistant-workflow.v2.template.json \
  "$ARTIFACTS/proactive-assistant-workflow.v2.json"
sudo -n -e "$ARTIFACTS/proactive-assistant-workflow.v2.json"
sudo -n install -o 10001 -g 10001 -m 0600 \
  tooling/deploy/teams-cloud/config/private-connector-bootstrap.github-only.template.json \
  "$ARTIFACTS/config/connector-bootstrap.json"
sudo -n -e "$ARTIFACTS/config/connector-bootstrap.json"

sudo -n python3 tooling/deploy/teams-cloud/preflight.py \
  --mode bootstrap \
  --compose-file tooling/deploy/teams-cloud/compose.yaml \
  --compose-file tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  --compose-file tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  --image-lock "$ARTIFACTS/build/image-lock.json" \
  --secrets-dir "$ARTIFACTS/secrets" \
  --config-dir "$ARTIFACTS/config"

sudo -n docker compose \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  -f tooling/deploy/teams-cloud/compose.yaml \
  -f tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  -f tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  up -d workspace-authority account-center github-issues teams-provider exchange
```

Run Exchange's local plan first. It verifies the Directory role and emits the actual GitHub profile reference without linking it or writing a connection. Read that owner-only plan privately, then add only the returned GitHub `profileRef` to `workspace_operator_profile_refs` in the private Account Center JSON supplied as `CYRENE_ACCOUNT_CENTER_CONFIGURATION_JSON`. Preserve its App and host-secret mappings; keep both `provider_secret_file_refs` and `hostSecretFileRefs` empty, and add no HF profile. Recreate Account Center so the explicit shared-profile allowlist is active before applying the bootstrap.

先运行 Exchange 本地计划命令。它会校验 Directory role 并返回真实 GitHub profile reference，但不会链接 profile 或写入 connection。私下核对仅 owner 可读的计划后，只把返回的 GitHub `profileRef` 加到 `CYRENE_ACCOUNT_CENTER_CONFIGURATION_JSON` 对应私有 Account Center 配置中的 `workspace_operator_profile_refs`。保留 App 与 host-secret 映射；`provider_secret_file_refs` 和 `hostSecretFileRefs` 都保持空数组/空对象，不添加 HF profile。随后重建 Account Center，使明确授权的共享 profile allowlist 生效，再执行 bootstrap。

```sh
install -d -m 0700 "$ARTIFACTS/acceptance"
umask 077
sudo -n docker compose \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  -f tooling/deploy/teams-cloud/compose.yaml \
  -f tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  -f tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  run --rm --no-deps exchange integrations bootstrap-connectors \
  --integration-database /data/integrations.sqlite3 \
  --private-config /run/config/connector-bootstrap.json \
  --plan-only > "$ARTIFACTS/acceptance/connector-bootstrap-plan.json"

# Add the exact returned GitHub profileRef; do not derive or guess it.
sudo -n -e "$ARTIFACTS/runtime.env"

sudo -n docker compose \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  -f tooling/deploy/teams-cloud/compose.yaml \
  -f tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  -f tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  up -d --force-recreate account-center

sudo -n docker compose \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  -f tooling/deploy/teams-cloud/compose.yaml \
  -f tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  -f tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  stop exchange

sudo -n docker compose \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  -f tooling/deploy/teams-cloud/compose.yaml \
  -f tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  -f tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  run --rm --no-deps exchange integrations bootstrap-connectors \
  --integration-database /data/integrations.sqlite3 \
  --private-config /run/config/connector-bootstrap.json

sudo -n docker compose \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  -f tooling/deploy/teams-cloud/compose.yaml \
  -f tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  -f tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  up -d exchange

temporary="$(mktemp)"
sudo -n docker compose \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  -f tooling/deploy/teams-cloud/compose.yaml \
  -f tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  -f tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  exec -T exchange \
  /app/Cyrene-Services/Cyrene-Exchange/product/.venv/bin/cyrene-exchange integrations connector-bindings \
  --integration-database /data/integrations.sqlite3 \
  --issuer "$EXPECTED_ISSUER" --subject "$OWNER_AAD_ID" \
  --organization-id "$ORGANIZATION_ID" --workspace-id "$WORKSPACE_ID" > "$temporary"
sudo -n install -o 10001 -g 10001 -m 0600 "$temporary" \
  "$ARTIFACTS/config/.connector-bindings.json.tmp"
sudo -n mv -f "$ARTIFACTS/config/.connector-bindings.json.tmp" \
  "$ARTIFACTS/config/connector-bindings.json"
rm -f "$temporary"

temporary="$(mktemp)"
sudo -n docker compose \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  -f tooling/deploy/teams-cloud/compose.yaml \
  -f tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  -f tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  exec -T exchange \
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

The apply command must return `directoryRoleVerified` and the real GitHub profile/connection identifiers. The two exporters read Exchange's durable state and live provider schemas; do not hand-create binding IDs, tool rows, or computer references. For ACP, the resulting launcher map/catalog must contain the approved GitHub and computer bindings and no Hugging Face entry. Then refresh the verified nine-repository source-workflow inventory, generate the matching provider/workflow allowlists, and replace pending GitHub target/workflow markers with the returned binding and verified source IDs. Stage 1 preflight requires the exact current bindings, repository scopes, and tool schemas.

apply 命令必须返回 `directoryRoleVerified` 和真实 GitHub profile/connection 标识。两个 exporter 从 Exchange 持久状态及实时 Provider schema 读取数据；不要手工创建 binding ID、tool 行或 computer 引用。ACP 导出的 launcher map/catalog 必须包含核准的 GitHub 与 computer binding，且没有 Hugging Face 项。随后刷新已核实的九仓库 source-workflow inventory，生成匹配的 Provider/workflow allowlist，并使用真实 binding 与已核实的 source ID 替换 pending GitHub target/workflow 标记。Stage 1 预检会要求当前 binding、仓库范围和工具 schema 精确匹配。

For CodeBuddy ACP, repeat preflight with `--mode stage1` and the same base, computer-runtime, and CodeBuddy Compose files plus the same runtime, image, CodeBuddy, and GitHub-only env files shown above; do not add the HF overlay. Then start the selected Stage 1 services with that same Compose/env selection. The complete host, image, owner/admission, repository, tool authorization, MCP, and secret-boundary checks still apply.

CodeBuddy ACP 路径在绑定和工具目录完成后，以相同的基础、computer-runtime、CodeBuddy Compose 文件，以及相同 runtime、image、CodeBuddy、GitHub-only env 文件运行 `--mode stage1` 预检；不要加入 HF overlay。随后使用同一 Compose/env 组合启动 Stage 1 服务。宿主机、镜像、owner/admission、仓库、工具授权、MCP 和 secret 边界检查仍全部适用。

```sh
sudo -n python3 tooling/deploy/teams-cloud/preflight.py \
  --mode stage1 \
  --compose-file tooling/deploy/teams-cloud/compose.yaml \
  --compose-file tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  --compose-file tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  --image-lock "$ARTIFACTS/build/image-lock.json" \
  --secrets-dir "$ARTIFACTS/secrets" \
  --config-dir "$ARTIFACTS/config"

sudo -n docker compose \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  -f tooling/deploy/teams-cloud/compose.yaml \
  -f tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  -f tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  --profile public-ingress up -d \
  navigator-persistence navigator teams-ingress teams-public-ingress
```

Stage 1 startup uses the same Compose/env selection and starts Navigator with `CYRENE_WORKFLOWS_ENABLED=false`. The owner-only workflow JSON is a preflight manifest under `.artifacts`; it is not mounted into Navigator. Preflight requires this candidate to remain `enabled:false` before and after runtime activation. The durable definitions live separately in the Navigator Persistence Work API, so the disabled-manifest check does not block an authorized API update or claim that the stored definition is disabled.

Stage 1 启动使用相同 Compose/env 组合，并以 `CYRENE_WORKFLOWS_ENABLED=false` 启动 Navigator。owner-only workflow JSON 是 `.artifacts` 下供预检使用的 manifest，不会挂载到 Navigator。运行激活前后，预检都要求该候选文件保持 `enabled:false`。持久 workflow definition 独立存储在 Navigator Persistence Work API；因此，disabled manifest 检查不会阻止经过授权的 API 更新，也不代表 live definition 已禁用。

Complete one owner Teams chat while the engine is off. Verify the resulting real Teams thread binding and durable Work Task ID, copy those exact values into the disabled host manifest, and rerun Stage 1 preflight. Before switching the engine on, query the actual Work API and require every stored v2 definition to be absent or disabled. If any stored definition is enabled, stop and resolve it through the authorized Work API before continuing. Only after the separate owner acceptance should you set `CYRENE_WORKFLOWS_ENABLED=true`, rerun Stage 1 preflight, and recreate Navigator. Then send the disabled host candidate with `enabled:true` in memory through the trusted owner writer, using the Work API `PUT`; immediately `GET` the collection and verify the same ID is stored as enabled. The `.artifacts` file remains disabled, so later preflight continues to validate the intended gate manifest.

Workflows engine 关闭期间，先完成一次 owner Teams 聊天。核实实际创建的 Teams thread binding 和持久 Work Task ID，把这两个真实值写入仍保持禁用的 host manifest，然后重新运行 Stage 1 预检。打开 engine 前，先查询真实 Work API，确认所有已存储 v2 definition 都不存在或为 disabled；若发现任何 enabled definition，先停下并通过获授权的 Work API 处理。只有单独通过 owner 验收后，才将 `CYRENE_WORKFLOWS_ENABLED=true`，重新运行 Stage 1 预检并重建 Navigator。随后通过可信 owner writer 调用 Work API `PUT`，在内存中把 disabled host candidate 的 `enabled` 改为 true；紧接着用 `GET` 回读 collection，核实同一 ID 已持久保存为 enabled。`.artifacts` 中的候选文件仍保持 disabled，因此后续预检继续验证预期的 gate manifest。

Read the persisted WorkFlow collection over the internal TLS service. The current Work API contract returns one unpaginated `{"items":[...]}` v2 collection. The script rejects any other top-level shape, pagination marker, non-v2 item, or non-boolean `enabled` value; if the API contract changes, update the reader to exhaust every page before enabling the engine. The command runs inside Navigator Persistence and reads its already-mounted owner-scoped session secret plus internal CA; it prints counts only, never the token or full workflow data:

通过内网 TLS 查询持久 WorkFlow collection。当前 Work API 契约以单个、不分页的 `{"items":[...]}` 返回 v2 collection。脚本会拒绝其他顶层结构、分页标记、非 v2 item 或非布尔 `enabled`；若 API 契约发生变化，必须先改为完整读取所有页再启用 engine。命令在 Navigator Persistence 容器内运行，从已挂载的 owner scope session secret 文件和内部 CA 读取凭据；只输出数量，不打印 token 或完整 workflow 内容：

```sh
sudo -n docker compose \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  -f tooling/deploy/teams-cloud/compose.yaml \
  -f tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  -f tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  exec -T -e WORKFLOW_WORKSPACE_ID="$WORKSPACE_ID" navigator-persistence \
  /opt/venv/bin/python - <<'PY'
import json
import os
import ssl
import urllib.parse
import urllib.request

workspace = urllib.parse.quote(os.environ["WORKFLOW_WORKSPACE_ID"], safe="")
url = f"https://navigator-persistence:8080/api/v1/workspaces/{workspace}/work/workflows?definitionVersion=2"
with open("/run/secrets/navigator-session-token", encoding="utf-8") as secret:
    token = secret.read().strip()
request = urllib.request.Request(
    url,
    headers={"Authorization": "Bearer " + token},
)
context = ssl.create_default_context(cafile="/run/secrets/internal-service-root-ca")
with urllib.request.urlopen(request, context=context, timeout=10) as response:
    collection = json.load(response)
if not isinstance(collection, dict) or set(collection) != {"items"}:
    raise SystemExit("Work API v2 collection response has an unknown or paginated shape")
items = collection["items"]
if not isinstance(items, list):
    raise SystemExit("Work API v2 collection items must be an array")
for item in items:
    if (
        not isinstance(item, dict)
        or not isinstance(item.get("id"), str)
        or not item["id"]
        or type(item.get("version")) is not int
        or item["version"] != 2
        or type(item.get("enabled")) is not bool
    ):
        raise SystemExit("Work API v2 collection contains an invalid workflow record")
if len({item["id"] for item in items}) != len(items):
    raise SystemExit("Work API v2 collection contains duplicate workflow IDs")
not_disabled = [item for item in items if item.get("enabled") is not False]
if not_disabled:
    raise SystemExit("Work API contains a non-disabled or invalid definition; keep the engine off")
print(json.dumps({"definitionCount": len(items), "disabledCount": len(items)}))
PY
```

After owner acceptance and the all-disabled read-back, change the one private ACP env value to `CYRENE_WORKFLOWS_ENABLED=true`, run the same `--mode stage1` preflight with the same Compose/env arguments, and recreate only Navigator. The preflight still reads the disabled host candidate; the enabled engine has no work to schedule until the approved definition is written.

owner 验收通过且 live definition 全部 absent/disabled 后，将私有 ACP env 中唯一的该项改为 `CYRENE_WORKFLOWS_ENABLED=true`；用相同 Compose/env 参数重新运行 `--mode stage1` 预检，再只重建 Navigator。此时预检仍读取 disabled host candidate；在写入获批的 enabled definition 前，engine 没有可调度的责任工作流。

```sh
sudo -n -e "$ARTIFACTS/codebuddy-acp.env"

sudo -n python3 tooling/deploy/teams-cloud/preflight.py \
  --mode stage1 \
  --compose-file tooling/deploy/teams-cloud/compose.yaml \
  --compose-file tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  --compose-file tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  --image-lock "$ARTIFACTS/build/image-lock.json" \
  --secrets-dir "$ARTIFACTS/secrets" \
  --config-dir "$ARTIFACTS/config"

sudo -n docker compose \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  -f tooling/deploy/teams-cloud/compose.yaml \
  -f tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  -f tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  up -d --force-recreate navigator
```

Activate only after that owner approval and engine restart. This command reads the still-disabled host candidate, changes only its in-memory `enabled` field, writes it over stdin to the internal Work API, then reads the stored v2 collection back. The service bearer remains inside Navigator Persistence; no token or workflow JSON is put in a command argument. The durable API update is separate from the unchanged disabled preflight manifest.

只在 owner 验收及 engine 重启完成后激活。命令读取仍为 disabled 的 host candidate，只在内存中改 `enabled` 字段，通过 stdin 写入内网 Work API，并立即回读持久 v2 collection。service bearer 留在 Navigator Persistence 容器内；token 与 workflow JSON 都不会放入命令参数。持久 API 更新与未改变的 disabled 预检 manifest 相互独立。

```sh
umask 077
temporary="$(mktemp "$ARTIFACTS/acceptance/workflow-activation.XXXXXX")"
sudo -n python3 - "$ARTIFACTS/proactive-assistant-workflow.v2.json" > "$temporary" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    workflow = json.load(source)
if workflow.get("enabled") is not False:
    raise SystemExit("host preflight candidate must remain disabled")
workflow["enabled"] = True
json.dump(workflow, sys.stdout, separators=(",", ":"))
sys.stdout.write("\n")
PY

sudo -n cat "$temporary" | sudo -n docker compose \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --env-file "$ARTIFACTS/codebuddy-acp.env" \
  --env-file "$ARTIFACTS/github-only.env" \
  -f tooling/deploy/teams-cloud/compose.yaml \
  -f tooling/deploy/teams-cloud/compose.computer-runtime.yaml \
  -f tooling/deploy/teams-cloud/compose.codebuddy-acp.yaml \
  exec -T -e WORKFLOW_WORKSPACE_ID="$WORKSPACE_ID" navigator-persistence \
  /opt/venv/bin/python -c '
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

workspace = urllib.parse.quote(os.environ["WORKFLOW_WORKSPACE_ID"], safe="")
base = f"https://navigator-persistence:8080/api/v1/workspaces/{workspace}/work/workflows"
workflow = json.load(sys.stdin)
if workflow.get("version") != 2 or workflow.get("enabled") is not True:
    raise SystemExit("activation payload must be an enabled WorkFlow v2 definition")
workflow_id = workflow["id"]
path = base + "/" + urllib.parse.quote(workflow_id, safe="") + "?definitionVersion=2"
context = ssl.create_default_context(cafile="/run/secrets/internal-service-root-ca")
with open("/run/secrets/navigator-session-token", encoding="utf-8") as secret:
    token = secret.read().strip()
headers = {"Authorization": "Bearer " + token}
body = json.dumps(workflow, separators=(",", ":")).encode()
request = urllib.request.Request(path, data=body, headers={**headers, "Content-Type": "application/json"}, method="PUT")
with urllib.request.urlopen(request, context=context, timeout=20) as response:
    saved = json.load(response)
if saved.get("id") != workflow_id or saved.get("enabled") is not True:
    raise SystemExit("Work API did not persist the enabled definition")
list_request = urllib.request.Request(f"{base}?definitionVersion=2", headers=headers)
with urllib.request.urlopen(list_request, context=context, timeout=20) as response:
    collection = json.load(response)
if not isinstance(collection, dict) or set(collection) != {"items"}:
    raise SystemExit("Work API activation read-back has an unknown or paginated shape")
items = collection["items"]
if not isinstance(items, list) or any(
    not isinstance(item, dict)
    or not isinstance(item.get("id"), str)
    or type(item.get("version")) is not int
    or item["version"] != 2
    or type(item.get("enabled")) is not bool
    for item in items
):
    raise SystemExit("Work API activation read-back contains an invalid v2 collection")
matches = [item for item in items if item.get("id") == workflow_id]
if len(matches) != 1 or matches[0].get("enabled") is not True:
    raise SystemExit("Work API read-back did not verify activation")
print(json.dumps({"workflowId": workflow_id, "enabled": True}))
'
rm -f "$temporary"
```

Do not start Stage 2/3 or cutover while HF is disabled. When HF authorization is approved, use `CYRENE_HF_ENABLED=true`, add `compose.huggingface.yaml`, and provide the real private OAuth seed plus the HF provider secrets, Account Center profile, Exchange bootstrap/profile, binding, and live catalog. Preflight rejects a missing seed or any incomplete HF gate.

HF 仍禁用时不得进入 Stage 2/3 或切换。HF 授权获批后，使用 `CYRENE_HF_ENABLED=true`、加入 `compose.huggingface.yaml`，并提供真实私有 OAuth seed、HF Provider secret、Account Center profile、Exchange bootstrap/profile、binding 和实时 catalog。缺 seed 或任一 HF 门槛不完整时，预检会拒绝。

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

For the native CodeBuddy ACP option, use `compose.yaml`, `compose.computer-runtime.yaml`, then `compose.codebuddy-acp.yaml`, with `config/codebuddy-acp.env.template` loaded after the private runtime env. Do not combine this overlay with `compose.proactive-assistant-v1.yaml`. It selects `CYRENE_HARNESS_EXECUTION_BACKEND=codebuddy-acp`, the exact `codebuddy` executable, and only `glm-5.3-flash` or `deepseek-v4.1-flash`; the legacy Harness model source is cleared and its fallback is disabled. The existing owner-only CodeBuddy CLI state remains in `secrets/codebuddy`; preflight verifies the directory owner/mode without reading its files or proving the vendor account identity. `CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP=true` is an owner deployment declaration. CodeBuddy's published CLI interface does not provide a verified control that prevents account-credit use, so do not treat that variable as a billing guarantee.

Navigator constructs the host-owned MCP bridge for each ACP run inside the container, writes its token-bearing configuration to a 0600 temporary file, passes only the path to `codebuddy`, and removes the file at termination. The profile supplies no MCP JSON, token, secret, or file mount through Compose. The CLI invocation remains Navigator-owned and uses stdio ACP, an empty built-in tools list, and strict MCP configuration. The overlay retains the base Navigator memory/CPU limits and adds the same read-only root, dropped capabilities, no-new-privileges setting, and bounded 128 MiB `/tmp` used by the existing proactive profile.

The Antigravity profile template is model-only: no shell tools, MCP servers, subagents, browser automation, or added plugins. Keep the CLI login/cache home at `CYRENE_MODEL_HOME` under the private task artifact directory, owned by UID 10001 and mode 0700. Keep Navigator task/session state in its persistent `navigator-sessions` volume. Keep the separate CodeBuddy provider profile under the private `secrets/codebuddy` path. Neither profile nor any provider token is copied into an image. Back up these profile directories and the business-state volumes separately, encrypt backups at rest, and retain the owner-only permissions when restoring.

The responsibility target file is host-managed configuration, not model output. Copy `config/responsibility-targets.v1.template.json` to the private `config/responsibility-targets.v1.json`; populate the ten exact public repository IDs from canonical `repositories.yaml` plus current GitHub App installation scope, then replace the unbound marker only after Exchange re-issues the GitHub profile binding for the complete approved set. Run `generate_github_workflow_allowlist.py --artifact-dir "$ARTIFACTS"` after the verified workflow inventory and ten-repository targets exist. It writes the same nine-repository IDs into the provider fragment `config/github-issues-source-workflow-ids.v1.json`, each Navigator target's `sourceWorkflowIds`, and the v2 workflow action. Merge only the fragment's `source_workflow_ids` member into the complete private GitHub provider JSON used for `CYRENE_GITHUB_ISSUES_CONFIGURATION_JSON`; preserve its existing App, Account Center, secret-reference, and repository configuration. Preflight compares the provider's exact ten-repository profile, this fragment, the verified inventory, and both Navigator grant layers; missing, extra, or mismatched IDs fail closed. This delivery bundle includes an owner-only generated list at `.artifacts/proactive-assistant-v1-20261024/config/responsibility-targets.v1.json`; its binding marker is intentionally unresolved because the existing production connector profile covers only a smaller scope. Do not enable the workflow or change the active runtime until the rebind is completed and read back. The target grant allows draft work only on `develop`, using task branches under `cyrene/assistant/`; failed source CI can be rerun only on those task branches and only for the explicit per-repository `sourceWorkflowIds` allowlist. The Workspace rerun grant is omitted because its CI workflow also creates immutable release artifacts. `pathPrefixes: ["**"]` means any repository-relative source path; the GitHub provider still independently denies hidden, control, deployment, and production paths. No issue update grant is provisioned.

The current GitHub App installation covers 19 repositories. Keep the provider profile and Navigator targets restricted to the ten approved Cyrene repositories; record narrowing the App installation itself as an operator follow-up before production. The approved DH-Skills acceptance profile remains a separate single-repository test and does not require changing App permissions in this deployment step.

Use `config/proactive-assistant-workflow.v2.template.json` as the disabled host preflight manifest. Populate its target IDs and current binding IDs from the host target file, and fill `authorization.threadBindingId` plus `authorization.ownerTaskId` from the freshly verified owner Teams thread. Keep `enabled:false`. This `.artifacts` file is not mounted into Navigator; preflight reads it from the host and requires it to stay disabled. Durable definitions are stored separately through the Navigator Persistence Work API. For CodeBuddy ACP, keep `CYRENE_WORKFLOWS_ENABLED=false` through the first owner chat, then query `GET /api/v1/workspaces/{workspaceId}/work/workflows?definitionVersion=2` and verify all stored v2 definitions are absent or disabled before switching the engine on. After separate owner approval, set the engine to true and restart Navigator, then submit the approved definition with `enabled:true` through `PUT /api/v1/workspaces/{workspaceId}/work/workflows/{id}?definitionVersion=2` and verify the stored state with another GET. The earlier GitHub-only section gives the exact private local commands. Later preflight checks still validate the unchanged disabled host manifest; it does not read the Work API or claim runtime activation state. The durable workflow polls GitHub every five minutes UTC; its first poll records a baseline without notification. It reviews all ten repositories at 09:00 and 17:00 America/New_York, sends at most one daily digest at 09:00, stays quiet on unchanged results from 22:00 to 08:00, and reports blocked goals, owner decisions, or service/auth outages immediately to the responsibility thread. It cannot merge, release, change production, or create new spend.

The computer runner exposes only four fixed model tools through an mTLS Caddy proxy on private Docker networks: `computer.command.execute`, `computer.file.read`, `computer.file.write`, and `computer.directory.list`. It does not expose artifact create/get tools. Its Exchange binding is optional host configuration: merge `config/computer-runner.integration-fragment.v1.template.json` into the generated `CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON` root, preserving existing connectors. Resolve the stable connection UUID, Directory organization/workspace IDs, tenant issuer, and certificate files from the live host before enabling it; the fragment's placeholders must never pass preflight. Compose provides Exchange with the existing plugin CA/client certificate/key paths and keeps the runner free of those credentials. The runner runs as UID/GID 10001 with a read-only root, no Linux capabilities, no credentials, no model profile, no Docker socket, and no external network. Its only writable paths are memory-backed scratch filesystems capped at 192 MiB for `/workspace`, 64 MiB for artifacts, and 64 MiB for `/tmp` (320 MiB total). These paths are ephemeral and are lost on runner restart or replacement; artifact references do not survive and must not be treated as durable. The runtime's internal 16 MiB artifact limit does not make artifact APIs available in this Teams v1 profile. Navigator task/session/operation state remains in Navigator Persistence; model and connector credentials remain in their existing service-owned persistent stores and are not mounted in the runner. The runner cgroup is capped at 512 MiB RAM and 0.10 CPU. Limits are 30-second default and 10-minute maximum command timeouts, 64 KiB per stdout/stderr stream, 64 KiB file reads, 1 MiB file writes, 2,048 directory entries, and depth 32. This is a low-privilege operational boundary, not a hostile-code sandbox: use CI for heavy builds and high-risk commands. V1 provides one shared workspace per runner and has no per-task filesystem isolation or call serialization. The scheduled responsibility workflow uses typed GitHub tools only; do not add background computer actions or permit concurrent computer tasks until Navigator/Exchange enforces single-writer coordination.

Before enabling background work, back up the current service configuration, image lock, Navigator persistence volume, DSH state, and provider auth directories into separate owner-only snapshots; verify the backup manifest and restore readability. Identify the old and new Navigator/DSH persistence schema versions, then use the supported export/import or migration for those exact versions in a cloned volume. Reconcile task, message, thread-binding, and event counts and exercise continuation from an existing Teams thread before cutover. Do not byte-copy a live database or assume an incompatible old volume can be mounted by the new image. If no supported migration exists, keep the old stack authoritative and defer cutover until a migration is implemented. Build and stage the new stack while the old stack remains recoverable. Do not allow both schedulers to run simultaneously: stop the old workflow scheduler only after the new stack passes health and chat checks, then enable the new workflow. On failure, disable the new workflow, stop its services, restore the previous config/state snapshot, and bring the old stack up; verify only one bot callback sender and one scheduler are active. Keep rollback credentials separate from business-state snapshots. The initial cutover, Teams callback, real repository reads, restart recovery, and 24-hour observation remain `NOT_RUN` until performed against the actual VM. The total write-acceptance budget is at most USD 5; each temporary test resource may run no longer than 15 minutes, and every created resource must be deleted and absence re-read.

当前 VM 的身份、容量、磁盘、Docker 状态、镜像构建/尺寸、Teams 回调和 24 小时稳定性均未在本轮查询，仍为 `NOT_RUN`。本轮只完成本地配置与打包准备；不得将文档中的限制或模板当成云端验收证据。

### 主动助手配置说明 / 中文说明

主动助手 v1 需要显式叠加 `compose.proactive-assistant-v1.yaml` 和 `compose.computer-runtime.yaml`。默认模型是 Antigravity `gemini-3.8-flash-medium`；CodeBuddy 只作为已配置的无额外付费回退。聊天、轮次、每日运行和修复次数都没有上限；并发槽位、单次供应商/工具超时和上游限制仍然生效。Navigator 持久化 Work Task 不设置默认总时限，因此用户聊天可与一个后台任务并行。

原生 CodeBuddy ACP 是独立可选项，按顺序叠加 `compose.yaml`、`compose.computer-runtime.yaml` 和 `compose.codebuddy-acp.yaml`，并在私有 runtime env 后加载 `config/codebuddy-acp.env.template`。不要与 `compose.proactive-assistant-v1.yaml` 叠加。它选择 `CYRENE_HARNESS_EXECUTION_BACKEND=codebuddy-acp`、精确的 `codebuddy` 可执行文件，以及 `glm-5.3-flash` 或 `deepseek-v4.1-flash`；旧 Harness model source 会清空，fallback 关闭。CodeBuddy CLI 状态继续保存在服务专属 owner-only `secrets/codebuddy` 中；预检验证目录 owner/权限，但不读取文件，也不能证明供应商账号身份。`CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP=true` 是 owner 的部署声明。CodeBuddy 已发布的 CLI 接口没有经过验证的设置可阻止账号 credits 被使用，因此不能把该变量当作扣费保证。

Navigator 在容器内为每次 ACP 调用创建 host-owned MCP bridge，将含 token 的配置写入权限为 0600 的临时文件，只把文件路径交给 `codebuddy`，并在调用结束后清理。Compose 不提供 MCP JSON、token、secret 或文件挂载。CLI 调用仍由 Navigator 固定，使用 stdio ACP、空的内置 tools 列表和 strict MCP 配置。该 overlay 保持基础 Navigator 的内存/CPU 上限，并采用现有主动助手的只读根目录、删除 capabilities、no-new-privileges 和有界 128 MiB `/tmp`。

Antigravity CLI 使用私有 task artifact 下 UID 10001 所有、权限为 0700 的 `CYRENE_MODEL_HOME`；CodeBuddy profile 保存在独立的私有 `secrets/codebuddy`；Navigator 会话和业务状态在持久化卷中。不要把任一供应商登录目录或 token 打进镜像。备份时分开保存并加密模型授权目录、Navigator 业务状态和服务配置，恢复后继续保持 owner-only 权限。

仓库目标配置只由宿主机维护。所有十个仓库的 ID 来自 Workspace 规范拓扑和当前 GitHub App 安装范围；现有生产 Exchange binding 范围较小，必须先重新绑定并回读后才能替换 `UNBOUND_PENDING_EXCHANGE_GITHUB_REBIND`。核实 workflow 清单和十仓库目标配置就绪后，运行 `generate_github_workflow_allowlist.py --artifact-dir "$ARTIFACTS"`。它会把同一组九仓库 CI workflow ID 写入 Provider 片段 `config/github-issues-source-workflow-ids.v1.json`、各 Navigator 仓库授权和 v2 工作流动作。将 Provider 片段的 `source_workflow_ids` 成员合并进 `CYRENE_GITHUB_ISSUES_CONFIGURATION_JSON` 对应的完整私有 JSON，保留现有 App、Account Center、secret refs 和仓库配置。预检会对照核实清单、Provider 精确十仓库 profile 和两层 Navigator grant；缺失、多余或不一致的 ID 都会阻止部署。当前 GitHub App 安装覆盖 19 个仓库；Provider profile 与 Navigator 目标仍限制为核准的十个 Cyrene 仓库。将缩小 App 安装范围记录为生产前运维后续；已批准的 DH-Skills 验收是独立单仓库测试，本阶段不要求修改 App 权限。

使用 `config/proactive-assistant-workflow.v2.template.json` 作为 disabled host preflight manifest。根据 host target file 填入 target ID 和 current binding ID；从已核实的 owner Teams thread 填入 `authorization.threadBindingId` 与 `authorization.ownerTaskId`，并保持 `enabled:false`。这个 `.artifacts` 文件不会挂载到 Navigator；预检从宿主读取且要求它保持禁用。持久 definition 通过独立的 Navigator Persistence Work API 保存。CodeBuddy ACP 在首次 owner 聊天期间保持 `CYRENE_WORKFLOWS_ENABLED=false`；启用引擎前，先用 `GET /api/v1/workspaces/{workspaceId}/work/workflows?definitionVersion=2` 查询真实 Work API，确认所有 v2 definition 均不存在或 disabled。owner 单独验收后，设置 engine 为 true 并重启 Navigator，再通过 `PUT /api/v1/workspaces/{workspaceId}/work/workflows/{id}?definitionVersion=2` 提交 `enabled:true` 的获批 definition，并用另一次 GET 核实持久状态。上方 GitHub-only 章节给出了精确的私有本地命令。后续预检继续检查未改变的 disabled host manifest；它不会读取 Work API，也不会声称 live runtime 状态。持久工作流每五分钟读取开放事项，首次只建立静默基线；每天纽约时间 09:00 和 17:00 汇总审查，每天最多推送一份 09:00 摘要，22:00 至 08:00 对未变化状态保持安静。阻塞、需要 owner 决策或服务/授权中断会立即发到责任帖子。仅允许 `develop` 到 `cyrene/assistant/` 下生成草稿 PR；重跑 CI 只允许明确列出的源 CI workflow 和同一命名空间内的分支。Workspace 候选 workflow 会产出不可变发行物，因此不授予自动重跑；Issue 状态更新、合并、发布、生产变更和新支出都不在默认授权内。

电脑执行服务位于单独的低权限容器中，通过内网 mTLS 代理只暴露四个模型工具：`computer.command.execute`、`computer.file.read`、`computer.file.write` 和 `computer.directory.list`；不暴露 artifact create/get 工具。Exchange 绑定属于可选的宿主配置：将 `config/computer-runner.integration-fragment.v1.template.json` 合并进已生成的 `CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON` 根对象，并保留现有连接器配置。启用前必须从实际主机核实连接 UUID、Directory 组织/工作区 ID、租户 issuer 和证书文件；片段中的占位值不能通过预检。Compose 通过现有只读挂载把插件 CA、Exchange 客户端证书和密钥路径交给 Exchange；运行器本身没有这些凭据。运行器 UID/GID 为 10001，根目录只读，无 Linux capability、无模型/供应商凭据、无 Docker socket、无外网。它的可写路径是总计 320 MiB 的内存 scratch 文件系统：`/workspace` 上限 192 MiB、artifact 64 MiB、`/tmp` 64 MiB。所有路径都是临时的，运行器重启或替换后内容会丢失；artifact 引用不会跨重启保留，也不能当成持久 artifact。运行时内部的 16 MiB artifact 限额不代表 Teams v1 profile 开放了 artifact API。Navigator Task/session/operation 状态仍保存在 Navigator Persistence；模型和连接器凭据仍由各自现有服务持久化管理，运行器不会挂载这些数据。运行器 cgroup 上限为 512 MiB 内存和 0.10 CPU。单条命令默认 30 秒、最多 10 分钟；stdout/stderr 各 64 KiB、单次文件读取 64 KiB、写入 1 MiB、目录最多 2,048 项/深度 32。该版本所有调用共享同一个工作目录，尚无按 Task 隔离或并发调用串行机制。主动责任工作流只用 GitHub typed tools；新增会调用电脑执行器的后台流程前，必须先由 Navigator/Exchange 强制单写者协调。高风险或大构建仍交给 CI。

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

### HF-enabled cold start: connector bootstrap before Navigator / 启用 HF 的冷启动：先配置连接器，再启动 Navigator

This section documents the optional HF-enabled path. It requires owner-approved HF authorization, the real OAuth seed, the HF provider secrets, and the current HF binding/catalog. For GitHub-only bootstrap and Stage 1, follow the preceding section and do not run the HF-specific bootstrap, service, or stage commands below. The two paths use distinct bootstrap inputs and Compose selections.

本节说明可选的 HF-enabled 路径，要求 owner 已批准 HF 授权，并准备真实 OAuth seed、HF Provider 凭据和当前 binding/catalog。GitHub-only bootstrap 与 Stage 1 请遵循前一节，不要执行下方 HF 专属 bootstrap、服务或阶段命令。两种模式使用不同的 bootstrap 输入和 Compose 文件组合。

Set `ARTIFACTS`, `OWNER_AAD_ID`, `EXPECTED_ISSUER`, `ORGANIZATION_ID`, and `WORKSPACE_ID` in the operator shell from the verified private deployment config. Do not source credential files or print their contents.

操作前从已核实的私有部署配置中，在运维 shell 设置 `ARTIFACTS`、`OWNER_AAD_ID`、`EXPECTED_ISSUER`、`ORGANIZATION_ID` 和 `WORKSPACE_ID`。不要 source 凭据文件，也不要打印其内容。

Provision the private `config/` and `secrets/` directories for the fixed container owner UID `10001` with mode `0700`. Every Compose-mounted config file must be owned by UID `10001`, mode `0400` or `0600`, and readable by that owner. The preflight checks these facts; an operator-owned `0600` file cannot be read by Navigator, Exchange, or Authority and is rejected. Runtime env/build receipts may remain with the host operator, but the mounted config/secrets directory tree is service-owned.

全量预检前，先为容器固定运行用户 UID `10001` 配置私有 `config/` 与 `secrets/` 目录，权限 `0700`。每个 Compose 挂载配置文件都必须归 UID `10001` 所有，权限为 `0400` 或 `0600`，且 owner 可读。预检会检查这些条件；运维用户所有的 `0600` 文件对 Navigator、Exchange 或 Authority 不可读，因此会被拒绝。runtime env/build 回执可以由主机运维用户持有，但挂载到服务的 config/secrets 目录树属于服务账号。

Create the bootstrap-only config files as the runtime UID, then edit the non-secret bootstrap values through the root editor. In this HF-enabled path, fill only the verified owner/workspace, GitHub App installation and repository IDs, stable idempotency keys, and approved HF target aliases. The file contains no tokens. Empty bindings and catalog files are only for dependency bootstrap; both are rejected by every staging, stage, and cutover preflight.

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
  --compose-file tooling/deploy/teams-cloud/compose.huggingface.yaml \
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
  -f compose.yaml -f compose.huggingface.yaml \
  up -d workspace-authority account-center github-issues huggingface teams-provider exchange
```

Use the Exchange one-shot to produce a plan. It obtains a fresh Directory grant and returns the actual stable profile references and connection UUIDs without linking an account or writing a connection. Read the output privately, then add the exact returned GitHub and HF `profileRef` values to Account Center's private `workspace_operator_profile_refs` list. Recreate Account Center so the explicit shared-profile opt-in is active before the actual link.

使用 Exchange 一次性命令生成计划。它会申请新的 Directory grant，并返回真实稳定的 profile reference 和 connection UUID，但不会链接账号或写入 connection。私下查看输出后，将其中精确的 GitHub、HF `profileRef` 添加到 Account Center 私有配置的 `workspace_operator_profile_refs` 列表；然后重建 Account Center，使显式共享 profile 授权生效，再执行实际链接。

```sh
install -d -m 700 "$ARTIFACTS/acceptance"
umask 077
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml -f compose.huggingface.yaml \
  run --rm --no-deps exchange integrations bootstrap-connectors \
  --integration-database /data/integrations.sqlite3 \
  --private-config /run/config/connector-bootstrap.json \
  --plan-only > "$ARTIFACTS/acceptance/connector-bootstrap-plan.json"

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml -f compose.huggingface.yaml up -d --force-recreate account-center
```

After verifying the planned refs and the active Account Center profile allowlist, run the same bootstrap once without `--plan-only`. Verify the returned `directoryRoleVerified`, healthy linked profiles, and connection IDs. Then export the real launcher map for the exact issuer, owner subject, and workspace to a same-directory temporary file and atomically replace `connector-bindings.json`. The exporter reads Exchange's durable SQLite rows; do not hand-create or guess connection IDs. Next, call the private Exchange `connector-tool-catalog` exporter. It queries the actual configured provider runtimes through Exchange's mTLS clients and returns only their tool schemas and safe metadata; it is a local CLI operation, not a debug HTTP endpoint. Replace `connector-tool-catalog.json` atomically with that output. Stage-1 preflight requires a non-empty matching GitHub/HF binding map and tool catalog; each tool ID and read/write bit must agree with the fixed binding policy.

核对计划引用以及 Account Center 已加载的 profile allowlist 后，再执行一次相同 bootstrap，移除 `--plan-only`。检查返回的 `directoryRoleVerified`、已链接且健康的 profile 和 connection ID。随后以精确 issuer、owner subject 和 workspace 导出 launcher map 到同目录临时文件，再原子替换 `connector-bindings.json`。导出器读取 Exchange 持久 SQLite 记录；不要手工创建或猜测 connection ID。接着调用 Exchange 私有 `connector-tool-catalog` exporter：它通过 Exchange mTLS 客户端查询实际配置的 Provider runtime，只返回工具 schema 和安全元数据；这是本地 CLI 操作，不是 debug HTTP endpoint。将输出原子替换到 `connector-tool-catalog.json`。Stage 1 预检要求 GitHub/HF 非空绑定与工具目录匹配；每个 tool ID 和读写标记必须符合固定绑定策略。

```sh
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml -f compose.huggingface.yaml stop exchange

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml -f compose.huggingface.yaml \
  run --rm --no-deps exchange integrations bootstrap-connectors \
  --integration-database /data/integrations.sqlite3 \
  --private-config /run/config/connector-bootstrap.json

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml -f compose.huggingface.yaml up -d exchange

umask 077
temporary="$(mktemp)"
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  -f compose.yaml -f compose.huggingface.yaml exec -T exchange \
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
  -f compose.yaml -f compose.huggingface.yaml exec -T exchange \
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
  --compose-file tooling/deploy/teams-cloud/compose.huggingface.yaml \
  --env-file "$ARTIFACTS/runtime.env" \
  --env-file "$ARTIFACTS/build/images.env" \
  --image-lock "$ARTIFACTS/build/image-lock.json" \
  --secrets-dir "$ARTIFACTS/secrets" \
  --config-dir "$ARTIFACTS/config"

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile public-ingress -f compose.yaml -f compose.huggingface.yaml up -d \
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
  --profile public-ingress -f compose.yaml -f compose.huggingface.yaml up -d

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile public-ingress --profile cloud-phase2 \
  -f compose.yaml -f compose.huggingface.yaml -f compose.cloud-phase2.yaml up -d

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile public-ingress --profile runpod \
  -f compose.yaml -f compose.huggingface.yaml \
  -f compose.cloud-phase2.yaml -f compose.runpod.yaml up -d
```

只有前一阶段回执已核验后，才启用下一阶段服务 profile。Stage 2 增加 Azure/GCP overlay。Stage 3 加载两个 overlay 文件，但只启用 `runpod` profile；它改为配置同一个 `cloud-management-phase2` 服务，并让持久云 Provider 状态卷保持唯一写入者。

```sh
sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile public-ingress -f compose.yaml -f compose.huggingface.yaml up -d

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile public-ingress --profile cloud-phase2 \
  -f compose.yaml -f compose.huggingface.yaml -f compose.cloud-phase2.yaml up -d

sudo -n docker compose --env-file "$ARTIFACTS/runtime.env" --env-file "$ARTIFACTS/build/images.env" \
  --profile public-ingress --profile runpod \
  -f compose.yaml -f compose.huggingface.yaml \
  -f compose.cloud-phase2.yaml -f compose.runpod.yaml up -d
```

The staged Caddy file uses an internal certificate and a high loopback-only HTTPS port. Only `teams-public-ingress` may publish a host port; the public route accepts `POST /api/messages` and rejects every other path or method. Navigator, Directory Authority, Exchange, Account Center, and providers remain on private Compose networks. Production publishes TCP 443 only, uses ACME TLS-ALPN, disables the HTTP-01 challenge, and leaves host port 80 unchanged and closed. The production preflight rejects `Caddyfile.staging` and `tls internal`. Bind to the public NIC address only after DNS, the Bot callback URL, and host port checks are ready. Preserve the old Docker stack during staging and rollback by stopping only the new ingress before restoring the old listener.

Staging Caddy 使用内部证书和仅监听 loopback 的高位 HTTPS 端口。只有 `teams-public-ingress` 可以映射宿主机端口；公网路由只接受 `POST /api/messages`，其他路径或方法均拒绝。Navigator、Directory Authority、Exchange、Account Center 和 Provider 仅在私有 Compose 网络中运行。生产只映射 TCP 443，使用 ACME TLS-ALPN，禁用 HTTP-01 challenge，并保持宿主机 80 端口不变且关闭。生产预检会拒绝 `Caddyfile.staging` 和 `tls internal`。只有 DNS、Bot 回调 URL 和宿主机端口检查就绪后，才绑定公网 NIC 地址。Staging 期间保留旧 Docker 栈；回滚时只停止新 ingress，再恢复旧监听。

### Host-Caddy-fronted canary

When an existing host Caddy already owns public TCP 443, keep its live configuration in place while preparing a bot-only candidate that forwards the existing `POST /api/messages` route to `https://127.0.0.1:18443`. Run `host_fronted_canary.py` only after the private Stage 1 stack is running. It reruns the canonical Stage 1 preflight, accepts an occupied port only after Docker read-back proves the unique loopback binding belongs to the same Compose project, `teams-public-ingress`, and its locked image, and requires all Stage 1 runtime services to be running and healthy.

The active host file is fixed at `/etc/caddy/Caddyfile`. The gate checks the systemd `MainPID`, executable, and `/proc` command line to confirm the service loads that file and does not use any `--resume` spelling; it then makes a fresh read-only `GET http://127.0.0.1:2019/config/` and requires its JSON to equal the active file's Caddy adaptation. The candidate must match the full adapted configuration except for the callback proxy's upstream and TLS transport. Its upstream and transport use a closed shape: one `127.0.0.1:18443` dial and one file-backed CA with the callback hostname as SNI. Additional roots, TLS exceptions such as `except_ports`, insecure verification, or other transport/upstream options block the gate. It also checks the current public certificate and requires a fresh successful native CodeBuddy ACP model probe. The current nested probe receipt must include its UTC completion timestamp and exit code; only legacy flat receipts may use the owner-only result file's modification time, and that time must follow the reviewed plan and remain fresh. A nested receipt missing its timestamp is blocked. The gate performs no writes, reloads, port changes, callback changes, or Teams messages. Its only success state is `CANARY_READY_NOT_ACCEPTED` with `deploymentReady:false`; it does not accept Teams conversations, connector business queries, cloud-provider operations, cutover, or the 24-hour observation. A blocked ACP login remains a hard canary blocker.

If `preflight.py` is invoked separately while the Stage 1 ingress is running, leave `--allow-owned-staging-ingress` unset by default. That explicit option is limited to `stage1`/`staging` and the locked Compose-owned `127.0.0.1:18443` binding. It does not apply to `cutover`, public port 443, another host port, or an unknown container owner.

### 宿主机 Caddy 前置 canary

若现有宿主机 Caddy 已占用公网 TCP 443，准备 bot-only 候选配置时应保留当前生效配置，并只将既有 `POST /api/messages` 路由转发到 `https://127.0.0.1:18443`。私有 Stage 1 服务运行后再执行 `host_fronted_canary.py`。该门禁会重新运行规范 Stage 1 预检；只有 Docker 回读确认 loopback 端口由同一 Compose project 中的 `teams-public-ingress` 和锁定镜像唯一占用，且所有 Stage 1 运行服务均已启动并健康，才接受端口被占用。

宿主机活动配置固定为 `/etc/caddy/Caddyfile`。门禁读取 systemd `MainPID`、可执行文件和 `/proc` 命令行，确认服务从该文件加载且没有使用任何 `--resume` 写法；随后只读请求 `GET http://127.0.0.1:2019/config/`，要求其 JSON 与活动文件经 Caddy 适配后的结果相等。候选配置必须与基线完整适配结果一致，只允许回调代理 upstream 和 TLS transport 变化；其 upstream/transport 使用封闭结构：仅一个 `127.0.0.1:18443` dial，且 TLS 只配一个文件型 CA 和同一回调域名 SNI。额外根证书、`except_ports` 等 TLS 例外、不安全验证选项或其他 upstream/transport 参数都会阻断。门禁也验证当前公网证书，并要求近期原生 CodeBuddy ACP 模型探测成功。当前嵌套探测回执必须包含 UTC 完成时间与退出码；仅旧版扁平回执可用 owner-only 结果文件修改时间兜底，并要求晚于已审核计划且保持新鲜。缺少时间戳的嵌套回执会阻断。门禁不写配置、不 reload、不改端口或回调地址，也不发送 Teams 消息。唯一成功状态为 `CANARY_READY_NOT_ACCEPTED` 且 `deploymentReady:false`；这不代表 Teams 对话、连接器业务查询、云 Provider 操作、生产切换或 24 小时观察已验收。ACP 登录被拒绝时，canary 必须保持阻断。

如果 Stage 1 ingress 正在运行时单独调用 `preflight.py`，默认不要设置 `--allow-owned-staging-ingress`。该显式选项仅适用于 `stage1`/`staging`，而且仅接受由锁定 Compose ingress 唯一占用的 `127.0.0.1:18443`。它不适用于 `cutover`、公网 443、其他宿主端口或未知容器所有者。

## Connector stages and budget / 连接器阶段与预算

1. **Teams, GitHub, and optional Hugging Face.** GitHub-only Stage 1 needs only the service-owned GitHub App key through Account Center. When HF authorization is approved, use a newly authorized device-OAuth bootstrap file; never copy the old personal OAuth token file. Keep the bootstrap under private onboarding storage and mount it read-only into Account Center only. Account Center persists profile credentials encrypted in its durable database; keep the source owner-only for restarts and reauthentication until a later config rollout removes its file reference and mount. Generate the Exchange connector map and live provider tool catalog from the verified owner/workspace. Verify private reads, a bounded API write, read-back, and deletion before enabling cloud-provider profiles.
2. **Azure and GCP.** Add only Azure and GCP profiles using the dedicated User Assigned Managed Identity and the GCP Workload Identity Federation config. Use the formal DSH → Exchange → DirectPlugin API path for the identity check, enumeration, detail, bounded create/lifecycle, status read-back, and cleanup. Native vendor CLIs may make read-only queries against those API-created resources; do not create a second copy through CLI.
3. **RunPod Pods.** Add the API key and RunPod profile only after the Azure/GCP API evidence is `VERIFIED`. Test Pod lifecycle, logs, readiness, and deletion through the formal API path. CLI probes remain read-only. Serverless is out of this stage.

1. **Teams、GitHub 和可选 Hugging Face。** GitHub-only Stage 1 只需通过 Account Center 使用服务专属 GitHub App 密钥。HF 授权获批后，使用新授权的 device-OAuth bootstrap 文件；不得复制旧个人 OAuth token 文件。bootstrap 文件只保存在私有 onboarding 目录，只读挂载到 Account Center。Account Center 会将 profile 凭据加密保存到持久数据库；在后续配置发布同时移除文件引用和挂载前，需将源文件保持 owner-only，以支持重启和重新授权。用已核验 owner/workspace 导出 Exchange 连接映射和真实 Provider 工具目录。依次验收私有读取、受限 API 写入、回读和删除，然后才开放云 Provider profile。
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
  --compose-file tooling/deploy/teams-cloud/compose.huggingface.yaml \
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
  --compose-file tooling/deploy/teams-cloud/compose.huggingface.yaml \
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

## Drain-aware Navigator and Exchange image updates / Navigator 与 Exchange 排空热更新

`runtime_update.py` supports only the standalone Teams deployment with a durable ingress spool. Its read-only `plan` command binds the active and candidate Compose/env/image-lock file hashes, exact running container IDs and immutable image IDs, source identity digests, Teams spool revision and dead-letter count, and the Navigator process generation. All CLI commands run as root so the plan, journal, and project lock share one operator identity. The root-owned private Workspace `.artifacts/` plan has mode `0600`; the persistent root-owned journal contains phase/status metadata but no bearer or drain tokens. Rootless plan/apply/status/resume is unsupported.

During `apply`, the Teams ingress keeps authenticating, durably storing, and acknowledging incoming activities while its delivery worker is paused. The updater waits for existing forwards to settle, drains Navigator, and replaces only the selected `exchange` and/or `navigator` service with `compose up --no-deps --pull never --no-build`. Exchange is updated before Navigator. Navigator's Compose stop grace is 40 seconds, covering the known 30-second executor-drain and 5-second context-cleanup defaults; the updater records Docker's actual old-container exit code and does not treat `137` as graceful. It verifies immutable image IDs and health, verifies that the old or new Navigator is accepting work, and only then resumes ingress delivery. This is a bounded maintenance window, not a zero-downtime promise.

If a candidate fails, rollback first establishes the operation's own ingress pause. If a candidate Navigator may have started, it must answer the drain API and prove `activeWorkDrained=true`, `activeTasks=0`, notification polling paused, and no notification drain in flight before Compose may replace it. When a Docker mutation has an unresolved outcome—including a process stop after its durable pre-call intent—the updater leaves the edge paused and records `RECOVERY_REQUIRED`; `resume` confirms the edge hold and does not repeat or roll back that mutation. Reconcile the exact Docker state before using a separate recovery action. Dead letters are never retried automatically; a non-zero `deliveryDeadCount` remains visible in the journal and requires the independent owner procedure.

The updater rejects RuntimeMaintenance/Broker markers, unknown running workers or service-network peers, additional Navigator/Exchange consumers, changed non-target images, and replacement of the Teams ingress. It does not update host Caddy, public ports, the ACP login, connector bindings, or provider profiles. Apply runs full Stage 1 preflight. Deterministic recovery repeats the static configuration and image-lock checks while skipping only host capacity thresholds, so a failed candidate cannot prevent restoration of the old locked image.

`runtime.env` must point `CYRENE_TEAMS_DELIVERY_STATE_DIR` at an already provisioned persistent host directory owned by UID `10001` with mode `0700`. Compose mounts it at `/var/lib/cyrene/teams-delivery`; the independent `teams-delivery-admin-token` file is a read-only secret mount into `teams-ingress` only and is checked by metadata without printing or loading its value into the host process. Preflight rejects a missing, symlinked, broadly accessible, or incorrectly owned spool directory.

The plan/apply split is intentional. Open a root shell on the VM before creating the private output directory. Plan, apply, status, and resume must all run as root; the root-private project lock is stored at `/run/cyrene-runtime-update/<project-hash>.lock` with mode `0600`. Review the plan digest before separately invoking apply:

```sh
DEPLOYMENT=tooling/deploy/teams-cloud
UPDATE_DIR="$ARTIFACTS/runtime-update"
install -d -o root -g root -m 0700 "$UPDATE_DIR"
umask 077

python3 "$DEPLOYMENT/runtime_update.py" plan \
  --active-compose-file "$ACTIVE_COMPOSE" \
  --active-env-file "$ACTIVE_RUNTIME_ENV" \
  --active-env-file "$ACTIVE_IMAGE_ENV" \
  --active-image-lock "$ACTIVE_IMAGE_LOCK" \
  --candidate-compose-file "$CANDIDATE_COMPOSE" \
  --candidate-env-file "$CANDIDATE_RUNTIME_ENV" \
  --candidate-env-file "$CANDIDATE_IMAGE_ENV" \
  --candidate-image-lock "$CANDIDATE_IMAGE_LOCK" \
  --secrets-dir "$ARTIFACTS/secrets" \
  --config-dir "$ARTIFACTS/config" \
  --docker-prefix docker \
  --target exchange --target navigator \
  --output "$UPDATE_DIR/plan.json"

python3 "$DEPLOYMENT/runtime_update.py" apply \
  --plan "$UPDATE_DIR/plan.json" \
  --confirm-plan-sha256 "$REVIEWED_PLAN_SHA256" \
  --journal-dir "$UPDATE_DIR/journal"

python3 "$DEPLOYMENT/runtime_update.py" status \
  --operation-id "$OPERATION_ID" \
  --journal-dir "$UPDATE_DIR/journal"

python3 "$DEPLOYMENT/runtime_update.py" resume \
  --operation-id "$OPERATION_ID" \
  --confirm-plan-sha256 "$REVIEWED_PLAN_SHA256" \
  --journal-dir "$UPDATE_DIR/journal"
```

For a `RECOVERY_REQUIRED` operation, use its exact operation ID and reviewed plan digest with `resume`. It will refuse recovery if the source files changed, the process/image identity is unknown, the edge hold cannot be proven, or the candidate Navigator cannot be drained. Keep the journal and spool intact until the state is reconciled.

`runtime_update.py` 仅支持带持久 ingress spool 的 standalone Teams 部署。只读 `plan` 会绑定 active/candidate Compose、env、image-lock 文件摘要，精确的运行容器 ID 和不可变镜像 ID，源码身份摘要，Teams spool revision/dead-letter 数量，以及 Navigator 进程 generation。所有 CLI 命令必须以 root 运行，使计划、journal 和项目锁共用一个 operator 身份。计划位于 root 私有 Workspace `.artifacts/` 目录，权限为 `0600`；持久 root-owned journal 仅包含阶段和状态元数据，不包含 bearer 或 drain token。不支持 rootless plan/apply/status/resume。

执行 `apply` 时，Teams ingress 仍会验证身份、持久保存并确认新活动，只暂停后台投递 worker。更新器等待已开始的转发完成，再排空 Navigator，随后只用 `compose up --no-deps --pull never --no-build` 替换选中的 `exchange` 和/或 `navigator` 服务。Exchange 先于 Navigator 更新。Navigator Compose stop grace 为 40 秒，覆盖已知 30 秒 executor drain 与 5 秒 context 清理预算；更新器记录旧容器真实 Docker exit code，不会将 `137` 视作正常退出。更新器回读不可变镜像 ID 和健康状态，确认旧/新 Navigator 已接受工作后才恢复 ingress 投递。这是有边界的维护窗口，不承诺零停机。

候选失败时，回滚先确认 ingress 处于本操作持有的暂停状态。如果候选 Navigator 可能已启动，必须通过 drain API 证明 `activeWorkDrained=true`、`activeTasks=0`、通知轮询已暂停且没有通知 drain 在途，Compose 才能替换它。Docker 操作结果未定（包括进程在持久化调用意图后退出）时，更新器保持 edge 暂停并记录 `RECOVERY_REQUIRED`；`resume` 只确认 edge hold，不会重放或回滚该 Docker 操作。先核对确切 Docker 状态，再使用独立恢复动作。dead-letter 不会自动重试；journal 中会保留非零 `deliveryDeadCount`，需要 owner 通过单独流程处理。

更新器拒绝 RuntimeMaintenance/Broker 标记、未知运行 worker 或网络 peer、额外 Navigator/Exchange consumer、非目标镜像变化以及 Teams ingress 替换。它不更新宿主机 Caddy、公网端口、ACP 登录、connector binding 或 Provider profile。apply 会运行完整 Stage 1 预检；确定性 recovery 会重复静态配置与镜像锁检查，只跳过主机容量阈值，避免候选造成的资源压力阻止旧锁定镜像恢复。

`runtime.env` 中的 `CYRENE_TEAMS_DELIVERY_STATE_DIR` 必须指向已预先创建、UID `10001` 所有且权限为 `0700` 的持久宿主机目录。Compose 将其挂载到 `/var/lib/cyrene/teams-delivery`；独立的 `teams-delivery-admin-token` 只读挂载到 `teams-ingress`，预检只核对元数据，不会向宿主进程读取或打印 token 内容。spool 目录缺失、为符号链接、权限过宽或 owner 不正确时预检会拒绝。

计划和应用分开执行。先在 VM 上打开 root shell，再创建私有输出目录。plan、apply、status、resume 都必须以 root 运行；root 私有项目锁位于 `/run/cyrene-runtime-update/<project-hash>.lock`，权限为 `0600`。审核计划摘要后再单独执行 apply：

```sh
DEPLOYMENT=tooling/deploy/teams-cloud
UPDATE_DIR="$ARTIFACTS/runtime-update"
install -d -o root -g root -m 0700 "$UPDATE_DIR"
umask 077

python3 "$DEPLOYMENT/runtime_update.py" plan \
  --active-compose-file "$ACTIVE_COMPOSE" \
  --active-env-file "$ACTIVE_RUNTIME_ENV" \
  --active-env-file "$ACTIVE_IMAGE_ENV" \
  --active-image-lock "$ACTIVE_IMAGE_LOCK" \
  --candidate-compose-file "$CANDIDATE_COMPOSE" \
  --candidate-env-file "$CANDIDATE_RUNTIME_ENV" \
  --candidate-env-file "$CANDIDATE_IMAGE_ENV" \
  --candidate-image-lock "$CANDIDATE_IMAGE_LOCK" \
  --secrets-dir "$ARTIFACTS/secrets" \
  --config-dir "$ARTIFACTS/config" \
  --docker-prefix docker \
  --target exchange --target navigator \
  --output "$UPDATE_DIR/plan.json"

python3 "$DEPLOYMENT/runtime_update.py" apply \
  --plan "$UPDATE_DIR/plan.json" \
  --confirm-plan-sha256 "$REVIEWED_PLAN_SHA256" \
  --journal-dir "$UPDATE_DIR/journal"

python3 "$DEPLOYMENT/runtime_update.py" status \
  --operation-id "$OPERATION_ID" \
  --journal-dir "$UPDATE_DIR/journal"

python3 "$DEPLOYMENT/runtime_update.py" resume \
  --operation-id "$OPERATION_ID" \
  --confirm-plan-sha256 "$REVIEWED_PLAN_SHA256" \
  --journal-dir "$UPDATE_DIR/journal"
```

若操作处于 `RECOVERY_REQUIRED`，使用其精确 operation ID 和审核过的 plan 摘要执行 `resume`。如果源码文件已变化、进程/镜像身份未知、edge pause 无法确认，或候选 Navigator 无法排空，resume 会拒绝继续。状态核实前保留 journal 和 spool。
