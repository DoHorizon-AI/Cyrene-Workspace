# Automatic Teams VM deployment / Teams VM 自动部署

The publication and deployment lanes are separate. GitHub Actions builds the six
managed ARM64 images from exact, green `develop` source commits. Azure Pipelines
verifies the immutable publication and invokes the existing VM update engine.
Azure does not check out sources, rebuild images, or run a second source test lane.

发布和部署职责分开：GitHub Actions 从各仓库当前 `develop` 的精确绿色提交构建六个 ARM64
镜像；Azure Pipelines 验证不可变发布后调用现有 VM 更新引擎，不重复检出、构建或做源码 CI。

```mermaid
flowchart LR
  A[Green canonical source CI] --> B[Native ARM64 build]
  B --> C[Immutable release and run-bound proof]
  C --> D[Verified HMAC event]
  D --> E[Azure CD artifact verification]
  E --> F[VM canonical plan and apply]
  F --> G[Actual images and connector reads]
  G --> H[Selector and observation read-back]
```

## Ownership and artifacts / 职责与发布物

| Component | Responsibility / 职责 |
| --- | --- |
| `release_bundle.py` | Resolve current green source heads, reuse `build_arm64`, and seal image archives, image lock and deployment tools / 解析当前绿色源码并封装镜像、锁与工具 |
| `.github/workflows/teams-cloud-release.yml` | Native ARM64 publication, with a fixed Docker/containerd format / 使用固定 Docker/containerd 格式在原生 ARM64 发布 |
| `.github/workflows/teams-cloud-cd.yml`, `azure_cd.py` | Verify exact publisher attempt and immutable proof, then HMAC-forward the release / 核验精确发布尝试后签名转交 |
| `ci/teams-cloud-cd.yml` | Azure WIF service connection and bounded VM RunCommand, followed by independent status read-back / 使用工作负载身份部署并独立回读 |
| `release_deploy.py` | Serialize existing per-target stage-transition transactions; derive status from their journals and selector / 串行复用正式单目标事务 |
| `deployment_probe.py` | Native, scoped, read-only acceptance through Navigator and Exchange, including optional RunPod / 通过原生链验证只读调用，可选包含 RunPod |
| `release_observation.py` | Preserve the old observation and establish a new version boundary / 保留原始观察并建立新版本边界 |
| `cyrene-teams-release-reconcile.*` | Resume only provably safe pending work on the VM / 仅自动续做已证明安全的待完成步骤 |

The release carries independent compressed Docker image archives. The VM downloads
and verifies one target at a time, rather than keeping a second complete stack in
memory or staging all images simultaneously. Source snapshots, image IDs, archive
hashes and the exact publisher run/attempt are pinned. A failed or incomplete
publication receives a new publication identity; an immutable tag is never rewritten.

发布包按镜像分开压缩，VM 逐项下载、核验、更新，不同时运行第二套服务或一次性存下全部
镜像归档。每版固定源码、镜像 ID、归档摘要和精确发布运行/尝试。失败或未完成发布可创建新
发布身份，绝不重写已发布 tag。

## One-time enablement / 一次性启用

1. Provision the host's private `deployment-policy.template.json` and
   `deployment-probe.template.json` at `/etc/cyrene/teams-cloud-deployment.json`
   and `/var/lib/cyrene-connectors-runtime/.artifacts/deployment-policies/teams-cloud-probe.json`.
   Both are root-owned mode 0600; the probe policy directory is root-owned mode 0700,
   and remains inside the private artifact tree required by the canonical loader. Derive
   the owner and read targets from the accepted Directory/Exchange configuration;
   never put provider credentials in the publication. Pin the probe policy SHA.
2. Provision the approved, same-generation four-file observation profile in a
   root-owned mode-0700 directory named by the hash of its source hash map. When
   adopting an existing observer, copy its exact selector-bound static receipt,
   verified against the active manifest, into that directory. Pin the profile in
   deployment policy; do not mix observer/generator/wrapper generations.
3. Create the UID 10001, mode-0700 probe output directory in the accepted private
   `.artifacts` tree. Keep the existing service credentials, volumes, configuration,
   TLS identities, resource limits and model authorization.
4. Configure the Azure incoming webhook connection `cyrene-teams-arm64-releases`
   with its dedicated HMAC secret and `X-cyrene-signature` header, and the AzureRM WIF connection
   `cyrene-teams-vm-arm-wif`. Grant the deployment identity only the required read
   and RunCommand actions on the existing VM. VM resize or new compute is not part
   of this lane.
5. Create an Azure pipeline for `ci/teams-cloud-cd.yml`, authorize only its two
   connections, and set GitHub secret `CYRENE_TEAMS_AZDO_WEBHOOK_SECRET` to the same
   dedicated HMAC value. Finally set repository variable
   `CYRENE_TEAMS_AZDO_CD_ENABLED=true`. The bridge stays disabled before this step.
6. Verify one real publication and cutover, a duplicate delivery, a safe
   pre-mutation retry and an explicitly uncertain-operation hold. Local fixtures
   and source CI are not VM or external-provider acceptance evidence.

一次性在服务端配置 owner、已绑定查询目标、私有策略文件、部署身份和 webhook。提供者凭据
继续留在原服务存储中，不搬进发布包。Azure 只取得现有 VM 必要的读取/RunCommand 权限，
不会扩容或创建计算资源。全部配置并验收后才开启 GitHub 启用变量；后续发布使用短期工作负载
身份，无需每次个人登录。首次验收必须是真实发布/VM 操作，不能拿本地模拟测试替代。

Source merges trigger Workspace publication. The scheduled source check also
detects changes in Navigator, Exchange and Plugins; an unchanged successful
publication is skipped. Manual dispatch uses the same current-green-source gates.
PRs, forks, arbitrary commits and failing or incomplete source CI cannot deploy.

Workspace 合并后触发发布；定时检查也会发现 Navigator、Exchange、Plugins 的变更。已有
完整成功发布时跳过重复构建。手动 dispatch 仍须通过同一套当前绿色源码检查；PR、fork、自由
提交参数、失败或未完成 CI 均不能触发部署。

## Runtime behavior / 运行行为

Each target receives a fresh native acceptance receipt and a separate immutable
stage-transition plan. Its candidate lock changes only that target row; all other
images and mounted configuration remain exact. An image-only overlay handles old
recovered aliases without editing existing environment files. The update engine
owns Teams queuing, Navigator drain, backups, health checks, rollback and atomic
selector promotion. Provider updates wait read-only for the normal authorization
monitor cycle; they do not disable the monitor or edit its schedule.

每个目标使用新鲜原生探测回执和独立正式计划，只改变一个镜像锁行。原环境文件、配置、身份
和其他镜像保持不变。旧镜像别名通过只含 image 的受控 overlay 处理。Teams 排队、Navigator
排空、备份、健康检查、回滚和 selector 原子提升由现有引擎负责。Provider 更新只读等待正常
授权检查周期，不关闭监视器或改它的时间表。

After cutover, actual running image IDs and health must match the selector. Native
HF/Azure/GCP operations are called again, and GitHub acceptance reads the scoped
repositories through the deployed GitHub connector. Results must match the
requested repository identities. Acceptance receipts bind the exact selector and
evidence byte hashes. A healthy process or exit code zero alone is insufficient.

When RunPod is selected, the probe issues only `cloud.provider.health.read` and
one `cloud.resources.list` call with the fixed `{"scope_ref":"pods"}` target and
no cursor. The provider bounds that first page to 100 items; this confirms a
first-page read and does not claim that the full account inventory was read.
RunPod health must report both authentication and authorization as `VALID` for
the configured host profile and identity. A successful transport carrying
`INVALID`, `REAUTHORIZATION_REQUIRED`, `PERMISSION_DENIED`, `UNKNOWN`, a missing
payload, or inconsistent structured/text projections fails acceptance.

切换后回读实际镜像及健康状态，并再次调用 HF/Azure/GCP API；GitHub 则通过已部署连接器读取
绑定仓库，逐项比对仓库身份。启用 RunPod 时，探针仅调用 `cloud.provider.health.read` 和一个
固定 `{"scope_ref":"pods"}`、不带 cursor 的 `cloud.resources.list`。Provider 将第一页限制为最多
100 项；这只证明第一页可读，不表示完整账户资源已读取。RunPod health 必须对配置的 host profile
和 identity 同时报出 authentication 与 authorization 为 `VALID`。传输成功但 payload 为 `INVALID`、
`REAUTHORIZATION_REQUIRED`、`PERMISSION_DENIED`、`UNKNOWN`、缺少 payload 或 structured/text 双投影不一致，均验收失败。验收回执固定 selector 和证据文件摘要，不能仅凭进程健康或退出码。

This is a serial, per-service rollout. If a later target fails, earlier completed
targets remain applied; the current target's canonical journal determines whether
rollback completed or recovery is required. Configuration, credential scope,
catalog-contract changes and noncompatible migrations are held rather than hidden
inside a normal image rollout. The default policy remains HF/Azure/GCP only. RunPod
is opt-in through `config/deployment-probe.runpod.template.json`; a real native
RunPod probe is required before activation is accepted. `--candidate-config-dir`
may generate notification provenance for a private candidate that preserves the
active owner, workspace, Teams route, Exchange profile, existing bindings and
accepted anchor, and adds one RunPod connection reference. The formal
stage-transition preflight verifies that reference against Exchange issuance.
This provenance is not a RunPod provider read or activation evidence.

该流程逐服务串行更新。后续目标失败时，前面已完成的目标保留；当前目标是否已回滚或需要恢复，
以正式日志为准。配置、授权范围、工具目录契约或不兼容迁移变更须明确停下处理，不能混入普通
镜像发布。默认策略仅包含 HF/Azure/GCP。RunPod 通过
`config/deployment-probe.runpod.template.json` 显式启用；接受激活前必须完成真实原生 RunPod 探测。
`--candidate-config-dir` 可为私有候选配置生成 notification provenance，但候选必须保留活动 owner、
workspace、Teams route、Exchange profile、现有 binding 和已验 anchor，并且只能增加一个 RunPod
connection reference。正式 stage-transition preflight 会核验该引用确由 Exchange 签发。该 provenance
不代表已调用 RunPod provider，也不作为激活验收。

| State | Meaning / 含义 |
| --- | --- |
| `APPLIED`, `NO_CHANGE` | Runtime, bound acceptance and the running or successfully completed observation are verified / 已核对运行、验收与正在运行或完整通过的观察 |
| `SAFE_TO_RESUME` | No unknown runtime mutation; preparation may continue / 无未决运行变更，可续做准备 |
| `ACCEPTANCE_PENDING` | Images are applied; complete missing read acceptance or observation startup / 镜像已应用，只补验收或观察启动 |
| `IN_PROGRESS` | Another controller still owns the process lock / 控制器仍持有进程锁 |
| `RECOVERY_REQUIRED` | Import/apply outcome is unknown; no replay / 导入或切换结果不明，禁止重放 |
| `FAILED` | A canonical operation reached a known failure/rollback / 正式操作已确认失败或回滚 |
| `SUPERSEDED` | A newer publication owns the selected runtime / 当前运行已属于更新发布 |

The five-minute VM reconciler uses the same private release bindings, canonical
plans/journals and selector. It retries safe downloads, waits and read probes; it
does not restart a known successful update or replay an uncertain Docker operation.
Existing credentials remain server-side. Download caches may be removed after
verified import/cutover; runtime images, backups and journals are retained.

VM 的五分钟协调任务读取相同的私有发布绑定、正式计划/日志和 selector。它可以续做安全下载、
等待和只读探测，不重复成功更新或重放未决 Docker 操作。只清理已验证的下载缓存，保留运行镜像、
备份、日志及全部服务端凭据。

## Stability evidence / 稳定性证据

An observation profile is a root-owned, hash-pinned copy of the approved four-file
health observer. A planned release closes and hashes the old sample/report files,
records its actual failures and incomplete duration, then starts a new baseline for
the new selector. A short preflight report is never a 24-hour PASS. Planned version
boundaries and unexpected failures remain distinct, inspectable history.

观察 profile 是已批准四个健康观察文件的 root 私有副本及固定摘要。计划发布时保留旧样本/报告、
真实失败数和未满时长，切换后为新 selector 开启新窗口。短预检不能算 24 小时通过；计划版本边界
和意外失败分别保留，不能重置样本掩盖问题。

The deployment lane does not promise zero response latency. Teams delivery remains
durable while dispatch is paused and drained, and models may still be slow or need
their own authorization recovery.

部署流程不承诺零延迟；切换时消息仍持久保存并等待派发，模型响应和模型自身授权仍需独立监测。

References: [GitHub workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows),
[Azure webhook resources](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/resources?view=azure-devops),
[Azure service identities](https://learn.microsoft.com/en-us/azure/devops/integrate/get-started/authentication/service-principal-managed-identity?view=azure-devops).
