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
