# Text Model Lifecycle V1 / 文本模型生命周期 V1

This is an implementation handoff for an independent validator. Local contract
and integration tests use tiny fixtures; no real training, GPU or complete
lifecycle acceptance is claimed by those tests. Run each action explicitly.
Products remain independently usable.

本文供独立验证者使用。本地契约与集成测试使用小型夹具，不代表真实训练、GPU 或
完整生命周期验收。逐项显式操作；各产品仍可独立使用。

## Operator setup / 运维准备

Use exact canonical checkouts and the reference profile in
[`runtime-profile-v1.md`](runtime-profile-v1.md). Set the three checkout roots
and `CYRENE_RUNTIME_HOME`, then run `scripts/reference-runtime bootstrap`.
The generated private manifest supplies Artifact Plane, Kernel socket, peer UID,
placement, signing key, trainer Python, and Reactor Host configuration.

使用精确 canonical checkout 和 reference profile，执行统一 bootstrap。生成的私有
manifest 负责 Artifact Plane、Kernel、UID、placement、签名密钥与 Python 环境路径。

V1 uses the existing Platform `LocalArtifactProvider` with **one operator-managed
Artifact root accessible to these services and the selected execution host**.
This is a provider deployment requirement, not an API file-path handoff. Set:

| Product | Configuration |
| --- | --- |
| Catalyst | `CYRENE_ARTIFACT_ROOT`, `CYRENE_YIELD_URL`; launch `uv run --no-sync python -m cyrene_catalyst` (port 8014) |
| Yield | `cyrene-yield --runtime-config $CYRENE_RUNTIME_HOME/platform/runtime.json --trainer-runtime-config $CYRENE_RUNTIME_HOME/trainer/runtime.json --state-directory ... --reactor-url ...` (port 8092) |
| Reactor host/control | Use generated `$CYRENE_RUNTIME_HOME/reactor/host.json` and `control.json`; ports 19301 and 19300 |
| Echo | `cyrene-echo --database ... --artifact-root ... --catalyst-url ...` (port 8094) |
| Navigator persistence | Add `--artifact-root ... --echo-url ...` to `scripts/serve-persistence.py`; retain the existing principal config and database |

All paths above are private operator configuration. Public resource identity is
always a Product URI or Artifact digest. A missing provider/receiver rejects the
action. There is no local-file, SSH-copy or hidden API fallback. Remote Artifact
replication is outside this V1 deployment profile.

Local development uses one orchestrator and one canonical data root
(`CYRENE_DEV_HOME`, default `~/.local/state/cyrene/dev`):

```bash
scripts/cyrene-dev.py init
scripts/cyrene-dev.py up       # reference runtimes, then the Product APIs
scripts/cyrene-dev.py status
scripts/cyrene-dev.py down     # releases services, runtimes, and recorded pids
```

`--scope runtimes|services` narrows any command. `up` stops previously recorded
services first, refuses to start the Product APIs when a runtime bootstrap
failed, and tears the stack down when a service never becomes ready. `down`
releases the Product services before the runtimes they depend on and never
deletes anything outside the data root.

`up` generates the operator credentials once into `$CYRENE_DEV_HOME/credentials.env`
(mode 0600) and prints the file. The lifecycle client reads the same names the
Products were started with, so an operator runs:

```bash
set -a; . "$CYRENE_DEV_HOME/credentials.env"; set +a
```

`scripts/text-lifecycle-v1.py acceptance` releases the gateway endpoint,
deployment, and training run when it passes; add `--keep-resources` only when
collecting evidence from a live route.

这里的路径仅用于服务启动配置，不进入产品资源身份。服务和执行端必须能访问同一
Artifact provider；未连接时明确失败。首版不新增远程 Artifact 复制机制。

Yield uses one NVIDIA GPU, FP32 SFT and one LoRA with the existing LLaMA-Factory
adapter. The trainer bootstrap installs its exact Python 3.12, PyTorch CUDA,
transformers, PEFT, gRPC, protobuf, and LLaMA-Factory lock, then verifies CUDA and
the Kernel descriptor before reporting READY.
The Product process does not run the trainer: Kernel starts the signed worker,
assigns the device, supervises its process tree and confirms lease release.
An unconfigured Kernel fails explicit start; it never falls back to local training.
WSL requires explicit `--allow-wsl-shared-device` and existing Platform admission.

Yield 执行端使用独立训练环境，安装本仓库 LLaMA-Factory 及相容的 CUDA/PyTorch、
grpcio、protobuf。由 Kernel 启动签名 Worker、分配 GPU、监管并回收资源。WSL 必须
显式准入；不宣称原生 Linux GPU 硬隔离验收。未配置 Kernel 时拒绝开始训练。

Set the public client origins in `CYRENE_CATALYST_URL`, `CYRENE_YIELD_URL`,
`CYRENE_REACTOR_URL`, `CYRENE_EXCHANGE_URL`, `CYRENE_NAVIGATOR_URL` (the persistence
service) and `CYRENE_ECHO_URL`. Optional `CYRENE_<PRODUCT>_TOKEN` variables supply
existing authenticated API scopes. Yield's outgoing Reactor credential is selected
by `--reactor-token-env CYRENE_REACTOR_TOKEN`. Provision credentials through the
existing private credential mechanism; never paste values into command arguments,
URLs, source or the selection file.

客户端配置产品 URL 与既有凭据引用即可；Yield 到 Reactor 的认证通过变量名称配置。
不得把凭据值粘贴到命令参数、URL、源码或选择文件中。Catalyst、Yield、Echo 的本地
服务应保持 loopback/已认证代理边界；统一身份授权是对公网发布前的阻塞项。

PUBLIC_RELEASE_BLOCKER: Connect Catalyst/Yield/Echo to authenticated, authorized
Product access before exposing their APIs to untrusted networks.

## Explicit user steps / 显式产品步骤

Run `python scripts/text-lifecycle-v1.py <action>` from Workspace. The client saves
returned selections in a mode-0600 file, not Product business state. Do not edit
that file; use `--selection another-selection.json` for a separate user activity.
Responses include target IDs, status and Open-in URLs. List actions show arrays;
selection indexes are one-based. No command automatically runs the next step.

以下每行均为单独的用户操作，命令前加 `python scripts/text-lifecycle-v1.py`。客户端
自动保存返回的资源选择，不需要手拼 ID 或手改 JSON。列表序号从 1 开始。

1. `dataset-import instruction.jsonl --name text-v1` — create Dataset and preparation.
2. `dataset-map` — select instruction/input/output fields; flags support other names.
3. `dataset-split --train-ratio 1`, then `dataset-confirm`, then `dataset-publish`.
4. `send-to-yield` — expect a Training draft, with no started TrainingRun.
5. `base-import --repository OWNER/MODEL --revision COMMIT`. Choose an immutable
   40-hex upstream revision. The bootstrap client publishes that snapshot directly
   to the configured Artifact Plane and carries its ArtifactRef into Yield. No live
   Reactor Host or ServingBinding exists at this training boundary.
6. `training-configure --template qwen --max-steps 2` — choose the template matching
   that base. These example debug settings are optional; select training settings
   deliberately. `training-start` is the explicit compute operation.
7. Read `training-result` until terminal. On COMPLETED, require `result.id`,
   `adapterArtifact`, `modelVersion.id`, BASE_PLUS_LORA, and training/dataset lineage.
   On failure inspect documented `/api/v1/training-runs/{id}/attempts`; no PASS is
   inferred from a trainer output directory. `training-cancel` requests cancellation.
8. `send-to-reactor` — expect Deployment DRAFT, no allocation. Run
   `serving-bindings`, then `deploy --name text-v1 --binding-index 1`.
   `deployment-status` must report READY.
9. `exchange-endpoint-create --name text-v1 --public-url URL --auth-policy-ref REF`
   creates a logical Exchange endpoint using the admitted operator policy. Read
   `exchange-receivers`, then `send-to-exchange --model text-v1` (select receiver and
   provider indexes if more than one). Expect Route DRAFT. `route-enable` explicitly
   confirms and probes through the existing Exchange lifecycle.
10. Open the existing Navigator Harness UI/client, choose the Exchange provider and
    route alias `text-v1`, and complete a conversation/Agent turn. Use its configured
    persistence workspace. `navigator-sessions --workspace WORKSPACE` lists sessions;
    `send-to-echo --workspace WORKSPACE --session-index 1` selects one explicitly.
11. `evaluation-input` shows the immutable selected outputs. `evaluate --reference-answer
    ANSWER` starts the existing exact-match evaluator with a user-supplied reference;
    repeat the flag for multiple samples in displayed order. A poor score is distinct
    from execution failure. API callers may select another existing EvaluationSuite.
12. `annotate --sample 1 --correction TEXT --score 1 --reviewer REVIEWER`, then
    `feedback-create --sample 1 --name selected-correction`. Only selected samples
    and annotations belong to that FeedbackSet.
13. `send-to-catalyst` prepares the next version in the selected original Dataset.
    Use `--new-dataset` only when explicitly desired. Expect a MAPPED preparation,
    with no published DatasetVersion and no retraining.
14. `dataset-split`, `dataset-confirm`, `dataset-publish` explicitly produce v2.
    Compare source refs, original model output, correction, annotation and score
    in the retained source Artifact. `deployment-stop` performs explicit cleanup.

完整路径：发布 v1 → Send to Yield → 选择基座/参数 → 显式训练 → Result/Adapter/
ModelVersion → Reactor 草稿 → 显式部署 → Exchange 草稿 → 显式启用 → Navigator
完成会话 → Echo 输入草稿 → 显式评估/人工标注/选择 FeedbackSet → Catalyst 准备 →
显式发布 v2。任何 Send to 都不会自动启动下一个执行步骤。

## Contracts and limits / 契约与限制

- Each Product publishes `/openapi.json` and `/docs`. Frozen lifecycle additions
  live in `contracts/product/v1/*openapi.yaml`. Navigator's persistence service
  uses `persistence.openapi.yaml`; it remains distinct from its aggregate API.
- Yield's implemented entry is POST `/api/v1/training-drafts`, PATCH the returned
  draft, then POST its `/actions/start`. The old declarative POST `training-runs`
  was never the server entry; it is no longer advertised by the frozen OpenAPI.
- PEFT `adapter_config.json` is authoritative for rank/alpha/targets. Published V1
  adapters contain that config and `adapter_model.safetensors`, validated before
  publication. Base source is the selected immutable repository/revision;
  tokenizer and chat template inherit from the base. Checkpoints use Artifact refs.
- Input is instruction/Alpaca JSONL. ShareGPT, multi-LoRA, multimodal, distributed
  training and other serving engines remain unsupported by this product profile.
- Navigator exports completed text outputs only, up to 1000 rows / 16 MiB, and
  checks the committed session revision. Tool/replay payloads remain in DSH history.
  The public API also supports explicit `selectedEventSeqs` and provenance refs.
- Feedback exports freeze on first export. Later annotation changes require a new
  explicitly selected FeedbackSet. Source artifacts retain original samples and
  human/evaluation evidence; Catalyst's normalized training export contains its
  intended instruction fields. No held-out sample enters training automatically.
- One active Yield Product owner per state directory. Restart attaches receipts
  without respawning training; incomplete/uncertain attempts remain blocked for
  explicit operator inspection. Multi-host failover and long GPU recovery acceptance
  are separate validation work.

首版只开放 instruction/Alpaca、单基座加单 LoRA、文本 vLLM。训练参数是用户意图，
训练产物中的 PEFT 配置仍为实际参数权威。Navigator 不复制工具内部状态；Echo 不
拥有会话历史，Catalyst 保留原始反馈 Artifact 与 lineage，再显式生成训练数据。

## Local targeted verification / 本地定向验证

Install Workspace's `text-lifecycle` dependency group and set
`CYRENE_{CATALYST,YIELD,REACTOR,NAVIGATOR,ECHO}_WORKTREE` individually to exact clean
checkouts. Run `uv run --no-sync pytest -q ci/text-lifecycle-v1`, then
`uv run --no-sync python ci/text-lifecycle-v1/validate_contracts.py` followed by the
five checkout roots. Missing checkouts fail; tests never substitute an IDE root.
Azure YAML checks locked dependencies and the pinned Platform checkout through an
existing service connection, without credential extraction. Hosted tests are not
GPU acceptance. Record actual commits, failures and skipped counts separately.

The `githubServiceConnection` parameter must identify a GitHub service connection
authorized for that pipeline and every declared repository resource. Resource
resolution and Product test results remain separate evidence states.

为五个产品分别设置精确检出路径，再运行 Workspace 定向测试和契约校验。Hosted CI
与 GPU 验收分开记录；不得用模拟训练结果代替真实训练，也不得将未运行标为 PASS。

当前既有连接不能用于跨仓库 resource。CI 管理员须为产品和 Workspace pipeline
授权有效 GitHub 服务连接，再通过 `githubServiceConnection` 参数选择它并复跑精确
提交。保持所有检查，不提取 checkout 凭据或绕过资源授权。
---
<!-- Chinese Translation / 中文翻译 -->

# 文本模型生命周期 V1

本文是交给独立验证者的实施交接说明。本地契约和集成测试使用 tiny fixture；这些测试不代表真实训练、GPU 或完整生命周期已验收。每项操作都必须显式执行，产品仍可独立使用。

## 运维准备

使用精确 canonical checkout 和 [runtime-profile-v1.md](runtime-profile-v1.md) 中的参考 profile。设置三个 checkout 根目录和 `CYRENE_RUNTIME_HOME`，然后运行 `scripts/reference-runtime bootstrap`。生成的私有 manifest 提供 Artifact Plane、Kernel socket、peer UID、placement、签名密钥、trainer Python 和 Reactor Host 配置。

V1 使用现有 Platform `LocalArtifactProvider`，由操作员管理一个 Artifact root，服务和选定的执行主机都能访问。此项是 provider 部署要求，不是通过 API 传递文件路径。各产品配置如下：

| 产品 | 配置 |
| --- | --- |
| Catalyst | `CYRENE_ARTIFACT_ROOT`、`CYRENE_YIELD_URL`；使用 `uv run --no-sync python -m cyrene_catalyst` 启动，端口 8014 |
| Yield | `cyrene-yield --runtime-config $CYRENE_RUNTIME_HOME/platform/runtime.json --trainer-runtime-config $CYRENE_RUNTIME_HOME/trainer/runtime.json --state-directory ... --reactor-url ...`，端口 8092 |
| Reactor host/control | 使用生成的 `$CYRENE_RUNTIME_HOME/reactor/host.json` 和 `control.json`，端口分别是 19301 和 19300 |
| Echo | `cyrene-echo --database ... --artifact-root ... --catalyst-url ...`，端口 8094 |
| Navigator persistence | 给 `scripts/serve-persistence.py` 增加 `--artifact-root ... --echo-url ...`；保留现有 principal config 和 database |

上述路径都是操作员私有配置。公开资源身份始终是 Product URI 或 Artifact digest。缺少 provider/receiver 时操作会失败；没有本地文件、SSH 复制或隐藏 API fallback。远程 Artifact replication 不在此 V1 部署 profile 范围内。

本地开发使用单一 orchestrator 和 canonical data root（`CYRENE_DEV_HOME`，默认 `~/.local/state/cyrene/dev`）：

```bash
scripts/cyrene-dev.py init
scripts/cyrene-dev.py up       # 先启动 reference runtimes，再启动 Product API
scripts/cyrene-dev.py status
scripts/cyrene-dev.py down     # 释放服务、运行时和已记录的 pid
```

任何命令都可用 `--scope runtimes|services` 缩小范围。`up` 会先停止先前记录的服务；runtime bootstrap 失败时拒绝启动 Product API；服务未 ready 时会拆除整个栈。`down` 先释放 Product service，再释放其依赖的 runtime，且绝不删除 data root 以外内容。

`up` 首次生成操作员凭据至 `$CYRENE_DEV_HOME/credentials.env`（权限 mode 0600）并打印文件路径。生命周期客户端读取与 Products 启动时相同的变量名，操作员需运行：

```bash
set -a; . "$CYRENE_DEV_HOME/credentials.env"; set +a
```

成功运行 `scripts/text-lifecycle-v1.py acceptance` 后会释放 gateway endpoint、deployment 和 training run。只有从仍在线的 route 收集证据时才加 `--keep-resources`。

上面这些路径只用于服务启动配置，不进入产品资源身份。服务和执行端必须访问同一 Artifact provider；未连接时明确失败。首版不新增远程 Artifact 复制机制。

Yield 使用一张 NVIDIA GPU、FP32 SFT 和一个 LoRA，并复用现有 LLaMA-Factory adapter。trainer bootstrap 安装精确锁定的 Python 3.12、PyTorch CUDA、transformers、PEFT、gRPC、protobuf 和 LLaMA-Factory 依赖，然后验证 CUDA 与 Kernel descriptor，才报告 READY。Product 进程不运行 trainer；Kernel 启动签名 Worker、分配设备、监督进程树并确认 lease 释放。Kernel 未配置时显式启动失败，永不回退到本地训练。WSL 要求显式 `--allow-wsl-shared-device` 以及现有 Platform admission。

Yield 执行端使用独立训练环境，安装本仓库 LLaMA-Factory 及兼容的 CUDA/PyTorch、grpcio 和 protobuf。Kernel 负责启动签名 Worker、分配 GPU、监督和回收资源。WSL 必须显式准入；不把它说成原生 Linux GPU 硬隔离验收。Kernel 未配置时拒绝开始训练。

为客户端配置公开服务地址：`CYRENE_CATALYST_URL`、`CYRENE_YIELD_URL`、`CYRENE_REACTOR_URL`、`CYRENE_EXCHANGE_URL`、`CYRENE_NAVIGATOR_URL`（persistence service）和 `CYRENE_ECHO_URL`。可选的 `CYRENE_<PRODUCT>_TOKEN` 提供现有认证 API scope。Yield 的 Reactor 出站凭据由 `--reactor-token-env CYRENE_REACTOR_TOKEN` 选择。通过现有私有凭据机制提供凭据；绝不把值放入命令参数、URL、源码或选择文件。

客户端只需配置产品 URL 和既有凭据引用；Yield 到 Reactor 的认证通过环境变量名选择。凭据值不得粘贴到命令参数、URL、源码或选择文件。Catalyst、Yield 和 Echo 的本地 service 应保持 loopback/已认证代理边界；对公网发布前，统一身份授权仍是阻塞项。

**公开发布阻塞项：** 在把 API 暴露给不受信网络前，必须让 Catalyst/Yield/Echo 接入经过认证和授权的 Product 访问方式。

## 显式产品操作步骤

从 Workspace 运行 `python scripts/text-lifecycle-v1.py <action>`。客户端将返回的 selections 保存到权限 mode-0600 文件，而非 Product 业务状态。不要编辑该文件；不同用户活动使用 `--selection another-selection.json`。响应包含目标 ID、状态和 Open-in URL。列表操作显示数组，selection 序号从 1 开始。每条命令都不会自动执行下一步。

以下命令均需在前面加上 `python scripts/text-lifecycle-v1.py`：

1. `dataset-import instruction.jsonl --name text-v1`：创建 Dataset 和 preparation。
2. `dataset-map`：选择 instruction/input/output 字段；其他字段名可用 flags 指定。
3. 依次执行 `dataset-split --train-ratio 1`、`dataset-confirm`、`dataset-publish`。
4. `send-to-yield`：应得到 Training draft，不应已经启动 TrainingRun。
5. `base-import --repository OWNER/MODEL --revision COMMIT`：选择不可变的 40 位十六进制上游 revision。bootstrap client 将该 snapshot 直接发布到配置的 Artifact Plane，并把其 ArtifactRef 传给 Yield。这个训练边界不存在活动的 Reactor Host 或 ServingBinding。
6. `training-configure --template qwen --max-steps 2`：选择与该 base 匹配的 template。示例 debug 参数是可选项，应审慎决定实际训练设置。`training-start` 才是显式计算操作。
7. 持续读取 `training-result` 直到终态。若为 COMPLETED，必须检查 `result.id`、`adapterArtifact`、`modelVersion.id`、`BASE_PLUS_LORA` 及 training/dataset lineage。失败时查看文档化的 `/api/v1/training-runs/{id}/attempts`；不得仅凭 trainer output directory 推断通过。`training-cancel` 会请求取消。
8. `send-to-reactor`：预期得到 Deployment DRAFT，不会分配资源。然后运行 `serving-bindings`，再执行 `deploy --name text-v1 --binding-index 1`。`deployment-status` 必须报告 READY。
9. `exchange-endpoint-create --name text-v1 --public-url URL --auth-policy-ref REF`：使用已准入的操作员策略创建逻辑 Exchange endpoint。读取 `exchange-receivers`，再运行 `send-to-exchange --model text-v1`（有多个 receiver/provider 时需选择 index）。预期生成 Route DRAFT；`route-enable` 会通过现有 Exchange lifecycle 明确确认并探测。
10. 打开现有 Navigator Harness UI/client，选择 Exchange provider 和 route alias `text-v1`，完成 conversation/Agent turn，并使用已配置的 persistence workspace。`navigator-sessions --workspace WORKSPACE` 列出会话；`send-to-echo --workspace WORKSPACE --session-index 1` 明确选择其中一个。
11. `evaluation-input` 显示不可变的已选输出。`evaluate --reference-answer ANSWER` 使用用户提供的 reference 启动现有 exact-match evaluator；多个样本按界面显示顺序重复该参数。分数低不等于执行失败。API 调用者也可选择其他现有 EvaluationSuite。
12. 先执行 `annotate --sample 1 --correction TEXT --score 1 --reviewer REVIEWER`，再执行 `feedback-create --sample 1 --name selected-correction`。只有所选样本及标注属于该 FeedbackSet。
13. `send-to-catalyst` 在所选原始 Dataset 中准备下一版本；只有明确需要时才使用 `--new-dataset`。预期得到 MAPPED preparation；不会发布 DatasetVersion 或重新训练。
14. 显式运行 `dataset-split`、`dataset-confirm`、`dataset-publish` 生成 v2。在保留的 source Artifact 中核对 source refs、原始模型输出、修订内容、标注和 score。最后可显式执行 `deployment-stop` 清理。

完整流程是：发布 v1 → 发送至 Yield → 选择基座/参数 → 显式训练 → Result/Adapter/ModelVersion → Reactor 草稿 → 显式部署 → Exchange 草稿 → 显式启用 → Navigator 会话 → Echo 输入草稿 → 显式评估/人工标注/选择 FeedbackSet → Catalyst preparation → 显式发布 v2。任一 Send to 都不会自动启动下一个执行步骤。

## 契约与限制

- 每个 Product 发布 `/openapi.json` 和 `/docs`。冻结的生命周期新增契约位于 `contracts/product/v1/*openapi.yaml`。Navigator persistence service 使用 `persistence.openapi.yaml`，与 aggregate API 保持独立。
- Yield 实现的入口是 POST `/api/v1/training-drafts`、PATCH 返回的 draft，再 POST 其 `/actions/start`。旧式声明 POST `training-runs` 从未是服务入口，冻结 OpenAPI 不再宣告它。
- PEFT `adapter_config.json` 是 rank/alpha/targets 的权威来源。发布的 V1 adapter 包含该配置和 `adapter_model.safetensors`，发布前需验证。Base source 是选定的不可变 repository/revision；tokenizer 和 chat template 继承自 base；Checkpoint 使用 Artifact refs。
- 输入为 instruction/Alpaca JSONL。ShareGPT、多 LoRA、多模态、分布式训练及其他 serving engine 均不受此产品 profile 支持。
- Navigator 最多导出 1000 行/16 MiB 的已完成文本输出，并检查 committed session revision。Tool/replay payload 仍保留在 DSH history。公共 API 也支持显式 `selectedEventSeqs` 和 provenance refs。
- Feedback 首次导出后冻结；后续 annotation 变更必须新建并明确选择 FeedbackSet。Source artifacts 保留原始样本和人工/评估证据；Catalyst 归一化训练导出只包含预期 instruction 字段。holdout 样本不会自动进入训练。
- 每个 state directory 只允许一个活跃 Yield Product owner。重启会 attach receipts，不会重新启动训练；不完整/不确定的 attempt 保持阻塞，等待操作员明确检查。多主机故障切换和长时间 GPU 恢复验收属于单独验证工作。

首版仅开放 instruction/Alpaca、单一基座加单 LoRA、文本 vLLM。训练参数表达用户意图，训练产物中的 PEFT 配置才是实际参数权威。Navigator 不复制工具内部状态；Echo 不拥有会话历史；Catalyst 保留原始反馈 Artifact 与 lineage，再显式生成训练数据。

## 本地定向验证

安装 Workspace 的 `text-lifecycle` dependency group，并将 `CYRENE_{CATALYST,YIELD,REACTOR,NAVIGATOR,ECHO}_WORKTREE` 分别设置为精确干净 checkout。运行 `uv run --no-sync pytest -q ci/text-lifecycle-v1`，再运行 `uv run --no-sync python ci/text-lifecycle-v1/validate_contracts.py` 并传入五个 checkout 根目录。缺失 checkout 会失败；测试不会拿 IDE root 替代。

Azure YAML 会在既有 service connection 下检查 locked dependencies 和 pinned Platform checkout，不提取凭据。Hosted tests 不属于 GPU 验收；应分别记录实际提交、失败和跳过数量。

`githubServiceConnection` 参数必须指向该 pipeline 及所有声明的 repository resources 均获授权的 GitHub service connection。Resource 解析结果和 Product test 结果是不同的证据状态。

当前连接不能用于跨仓库 resource。CI 管理员需为产品和 Workspace pipeline 授权有效 GitHub service connection，再通过 `githubServiceConnection` 参数选择并对精确提交重跑。保留全部检查，不提取 checkout 凭据，也不绕过资源授权。
