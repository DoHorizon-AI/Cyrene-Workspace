# CYRENE_TEXT_LIFECYCLE_V1_LOCAL_GPU

This is the Public Alpha reference runtime profile for one machine and one
NVIDIA GPU. Canonical GPU acceptance uses a trusted Azure DevOps self-hosted
agent on the RTX 4080 node. Linux is the runtime OS. If that node is a Windows
host running the agent in WSL2, evidence must say `HOST=Windows` and
`GPU_RUNTIME=WSL2_CUDA`; it is not native Linux isolation.

The supported production mode is `NATIVE_LINUX_PROFILE`. It requires an NVIDIA
driver compatible with the locked Yield CUDA 12.8 and Reactor CUDA 13.0 runtimes,
delegated cgroup v2 access for `sandboxd`, Python 3.12, Rust, `uv`, and sufficient
free GPU memory for the 10 GiB Reactor admission floor. `WSL_DEV_PROFILE` is an
explicit developer-only mode. It turns
on the existing WSL shared-device admission and sandbox development mode, and
reports `hardIsolation=false`. It does not change the 10 GiB production default.

| Layer | Canonical component or contract |
| --- | --- |
| Platform runtime manifest | `CYRENE_PLATFORM_RUNTIME_V1_LOCAL_GPU` |
| Artifact Plane | Platform `LocalArtifactProvider`, one runtime-owned root |
| Hardware | `cyrene-nvidia-adapter`, service-account UID policy |
| Sandbox | `cyrene-sandboxd`, service-account UID policy |
| Execution | `cyrene-kernel`, UDS worker/provider control, signed installations |
| Placement | Exact-source `cyrene-reactor-host-placement`, installed in runtime home |
| Training | Yield locked Python 3.12 / PyTorch CUDA / LLaMA-Factory bundle |
| Serving | Reactor locked Python 3.12 / vLLM bundle, Host on loopback port 19301 |
| Control | Reactor Product control on loopback port 19300 |

Socket names, peer UIDs, signing-key paths, Cargo output paths, and Python
environment paths are private manifest fields. Products consume the generated
runtime configs. Public acceptance evidence contains only profile, mode,
component status, source revisions, package versions, CUDA facts, and binary or
protocol digests.

Startup order is NVIDIA adapter → sandboxd → Kernel → trainer probe → placement
and serving probe → Reactor Host → Reactor control → remaining Products.
Shutdown stops Product traffic and deployments first, then Reactor processes,
then Kernel → sandboxd → NVIDIA adapter. Pipeline teardown runs under `always()`.

```bash
export CYRENE_PLATFORM_WORKTREE=/opt/cyrene/Cyrene-Platform
export CYRENE_YIELD_WORKTREE=/opt/cyrene/Cyrene-Yield
export CYRENE_REACTOR_WORKTREE=/opt/cyrene/Cyrene-Reactor
export CYRENE_RUNTIME_HOME=/srv/cyrene/reference-runtime
scripts/reference-runtime bootstrap
scripts/reference-runtime status
```

The self-hosted agent must be restricted to trusted pipelines, use a dedicated
least-privilege service account and work directory, and receive secrets only
through secret pipeline variables. Fork pull requests cannot enter GPU jobs.

## 中文

这是单机单 NVIDIA GPU 的 Public Alpha reference profile。正式 GPU 验收由受信任
的 Azure DevOps RTX 4080 self-hosted Agent 执行。运行时操作系统为 Linux；若物理
主机为 Windows、Agent 在 WSL2 中运行，证据必须记录 `HOST=Windows`、
`GPU_RUNTIME=WSL2_CUDA`，不能标为原生 Linux 硬隔离。

生产模式为 `NATIVE_LINUX_PROFILE`，要求兼容 CUDA 12.8 的驱动、可委托的 cgroup
v2、Python 3.12、Rust、`uv`，并保留 Reactor 10 GiB 显存准入下限。
`WSL_DEV_PROFILE` 只能显式选择，会报告 `hardIsolation=false`，不会降低生产默认值。

所有 socket、peer UID、签名密钥、Cargo 输出及 Python 环境路径均由私有 manifest
生成并传给 Product。验证者只需指定三个仓库 checkout 与一个 runtime home，执行
bootstrap 和 status；失败会关闭本轮已启动的 Platform 进程。
