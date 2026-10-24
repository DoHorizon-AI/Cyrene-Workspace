# Teams cloud deployment bundle / Teams 云端部署包

This directory contains the guarded ARM64 build, runtime composition, host preflight,
and bounded cloud-acceptance helpers for the Cyrene Teams workload.

本目录提供 Cyrene Teams 工作负载的受控 ARM64 构建、运行编排、主机预检和有预算上限的云端验收工具。

| File | Responsibility |
| --- | --- |
| `compose.yaml` | Private service network and the single Teams callback ingress / 内网服务与唯一 Teams 回调入口 |
| `compose.cloud-phase2.yaml`, `compose.runpod.yaml` | Opt-in Azure/GCP then RunPod on the same single cloud-provider service / 在同一个云 Provider 服务上按阶段启用 Azure/GCP 和 RunPod |
| `Dockerfile.*` | Source-built ARM64 service images with fixed runtime entrypoints / 从固定源码构建 ARM64 服务镜像并锁定运行入口 |
| `config/requirements.huggingface-cli.lock` | Hash-locked ARM64/Python 3.12 `hf` CLI dependency closure / 固定哈希的 ARM64/Python 3.12 `hf` CLI 依赖闭包 |
| `config/acceptance-plan.schema.json`, `config/acceptance-plan*.template.json`, `config/acceptance-evidence.template.json`, `config/cloud-api-evidence.*` | USD 5 cumulative reservations, closed real-API stage receipts, and exact cleanup actions / 5 美元跨阶段预算、正式 API 阶段回执和精确清理动作 |
| `config/connector-bindings*.json`, `config/connector-tool-catalog*.json` | Bootstrap-only empty maps plus production schemas for Exchange-issued bindings and live provider tool catalogs / 仅限引导阶段的空映射，以及 Exchange 绑定与实时 Provider 工具目录的生产 Schema |
| `build_arm64.py` | Fetch exact remote commits, build ARM64 images, and record image IDs / 获取精确远端提交、构建 ARM64 镜像并记录镜像 ID |
| `transfer_arm64.py` | Enforce strict SSH/capacity gates and stream locked images directly into Docker / 严格核验 SSH 和容量门槛，并将锁定镜像直接流式导入 Docker |
| `preflight.py` | Read-only host, image, config, secret, stage-evidence, tool-catalog, and ingress checks / 只读核验主机、镜像、配置、密钥、阶段回执、工具目录和入口 |
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

只从干净且已提交的 Workspace 检出目录执行源码构建，并为每个仓库提供精确源 SHA。构建器把远端 Git 提交快照到临时目录；不会复制同级脏工作树或用户主目录中的文件。

The base Compose file publishes only TCP 443 for the callback ingress. Exchange,
Navigator, Directory Authority, Account Center, and every DirectPluginRuntime
remain on private networks; providers use distinct service certificates, a
private client-CA bundle, and mTLS health checks. The public Caddyfile accepts
only `POST /api/messages`, uses the ACME TLS-ALPN challenge, and leaves host port
80 closed.

Hugging Face uses a fresh device-OAuth bootstrap selected by this production
bundle. Its private JSON file stays outside the secret directory and is mounted
read-only into Account Center only. Account Center persists profile credentials
encrypted in its durable database; retain the bootstrap file owner-only for
restarts and reauthentication until a later config rollout removes both the
file reference and mount. The preflight validates the closed DTO and target
repository scope without printing token values.

基础 Compose 文件只为回调入口发布 TCP 443。Exchange、Navigator、Directory Authority、Account Center 和所有 DirectPluginRuntime 均留在私有网络；Provider 使用各自服务证书、私有 client-CA bundle 和 mTLS 健康探针。公网 Caddyfile 只接受 `POST /api/messages`，使用 ACME TLS-ALPN challenge，并保持宿主机 80 端口关闭。

本生产部署包固定使用新授权的 Hugging Face device-OAuth bootstrap。私有 JSON 文件保存在 secrets 目录之外，只读挂载到 Account Center。Account Center 会将 profile 凭据加密保存到持久数据库；在后续配置发布同时移除文件引用和挂载前，需将 bootstrap 文件保持 owner-only，以支持重启和重新授权。预检会校验封闭 DTO 与目标仓库范围，不会输出 token 内容。
