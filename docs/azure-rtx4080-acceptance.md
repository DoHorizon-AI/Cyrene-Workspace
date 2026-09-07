# Azure DevOps RTX 4080 acceptance environment

Use a dedicated self-hosted Agent Pool with the capability
`cyrene.gpu=rtx4080`. The agent service account owns one private work directory
and the configured runtime home. It must have no personal SSH, GitHub, or Azure
credentials. Repository checkout uses the pipeline's `DoHorizon-AI` GitHub
service connection; runtime secrets arrive only as secret variables or secure
files and are never included in diagnostic environment dumps.

Only trusted pipelines may use the pool. Disable job authorization for public
fork pull requests, do not grant the pool to project-wide contributor groups,
and scope pipeline identities to read the required repositories. GPU jobs also
check `Build.Reason != PullRequest`, canonical branch names, and the RTX 4080
agent capability. These checks supplement Azure pool authorization.

Set the host evidence explicitly:

- Native Linux host: `HOST=Linux`, `GPU_RUNTIME=NATIVE_LINUX_CUDA`.
- Windows host with a WSL2 agent: `HOST=Windows`, `GPU_RUNTIME=WSL2_CUDA`.

Do not report a WSL2 agent as native Linux isolation. Canonical release
acceptance should use `NATIVE_LINUX_PROFILE`; WSL2 is retained as explicit
development evidence until the release policy accepts otherwise.

The Workspace pipeline has two layers. Hosted jobs run lint, contracts,
schemas, unit tests, clean-room builds, and non-GPU integration on pull requests
and branch updates. The parameterized self-hosted job is unavailable to pull
requests and provisions the full Platform/Yield/Reactor runtime from exact
checkouts. A Validation Captain then runs the documented explicit lifecycle on
that same trusted node. Full lifecycle evidence is required for a release
candidate and remains distinct from runtime bootstrap evidence.

Before every GPU run, call `scripts/reference-runtime down` on the stable agent
runtime home, then `bootstrap` and `status`. Stop deployments and Product
processes before calling `down` during teardown. Azure steps must use `always()`
for teardown so a failed training or serving probe still attempts cleanup.

## Resource authorization

The GitHub service connection is `DoHorizon-AI`. Authorize it for these Azure
pipeline definitions and repository resources:

| Pipeline | Definition ID | Repository resources |
| --- | ---: | --- |
| Cyrene-Reactor | 21 | `DoHorizon-AI/Cyrene-Platform` |
| Cyrene-Yield | 22 | `DoHorizon-AI/Cyrene-Platform` |
| Cyrene-Workspace | 23 | Platform, Catalyst, Yield, Reactor, Exchange, Navigator, Echo |

An Azure Project Administrator or Endpoint Administrator should open **Project
Settings → Service connections → DoHorizon-AI → Security**, grant **Use** to
the three pipeline build-service identities, then open each pipeline's
**Settings → Resource authorizations** and authorize the listed repository
resources. Do not rewrite YAML or add embedded Git credentials when a run is
canceled during YAML/resource resolution. Rerun the same commit after
authorization and record whether a job actually started.

Pool and service-connection inventory also require Azure DevOps authenticated
API access. A caller that receives `requires user authentication` cannot attest
agent presence, pool authorization, or endpoint permissions from pipeline YAML
alone.

## 中文

RTX 4080 节点使用独立 self-hosted Agent Pool，并设置
`cyrene.gpu=rtx4080` capability。Agent 由最小权限服务账户运行，不携带个人 SSH、
GitHub 或 Azure 凭据；源码只通过 `DoHorizon-AI` Service Connection 检出，秘密只通过
Pipeline secret/secure file 注入。

GPU Pool 只授权受信任 Pipeline，fork PR 不得自动进入。Windows+WSL2 必须记录
`HOST=Windows`、`GPU_RUNTIME=WSL2_CUDA`，不得写成原生 Linux 硬隔离。每次运行先
对固定 runtime home 执行 down，再 bootstrap/status；失败路径也必须通过 `always()`
teardown。资源解析阶段取消时按上表授权 Service Connection 与 Repository Resource，
不要反复改 YAML 或嵌入 Git 凭据。
