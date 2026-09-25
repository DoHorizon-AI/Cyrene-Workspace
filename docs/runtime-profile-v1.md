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
---
<!-- Chinese Translation / 中文翻译 -->

# CYRENE_TEXT_LIFECYCLE_V1_LOCAL_GPU

这是面向单机和单块 NVIDIA GPU 的 Public Alpha 参考运行时配置档。规范 GPU 验收由 RTX 4080 节点上的受信任 Azure DevOps self-hosted Agent 执行。运行时操作系统是 Linux。如果该节点是运行 WSL2 Agent 的 Windows 主机，证据必须写明 `HOST=Windows` 和 `GPU_RUNTIME=WSL2_CUDA`；这不属于原生 Linux 隔离。

受支持的生产模式为 `NATIVE_LINUX_PROFILE`。它要求 NVIDIA 驱动兼容已锁定的 Yield CUDA 12.8 和 Reactor CUDA 13.0 运行时；`sandboxd` 可获得委托的 cgroup v2 访问权限；安装 Python 3.12、Rust 和 `uv`；并且 GPU 有足够可用显存满足 Reactor 10 GiB 准入下限。`WSL_DEV_PROFILE` 是必须显式选择的开发者专用模式。它启用现有 WSL 共享设备准入和沙箱开发模式，并报告 `hardIsolation=false`。它不会改变生产环境 10 GiB 的默认值。

| 层 | 规范组件或合约 |
|---|---|
| Platform 运行时 manifest | `CYRENE_PLATFORM_RUNTIME_V1_LOCAL_GPU` |
| Artifact Plane | Platform `LocalArtifactProvider`，一个由运行时拥有的根目录 |
| 硬件 | `cyrene-nvidia-adapter`、服务账号 UID 策略 |
| 沙箱 | `cyrene-sandboxd`、服务账号 UID 策略 |
| 执行 | `cyrene-kernel`、UDS worker/provider 控制、签名安装 |
| 放置 | 精确源码构建的 `cyrene-reactor-host-placement`，安装在 runtime home 中 |
| 训练 | Yield 锁定的 Python 3.12 / PyTorch CUDA / LLaMA-Factory bundle |
| 服务 | Reactor 锁定的 Python 3.12 / vLLM bundle；Host 监听 loopback 端口 19301 |
| 控制 | Reactor Product 控制接口监听 loopback 端口 19300 |

Socket 名称、peer UID、签名密钥路径、Cargo 输出路径和 Python 环境路径都是私有 manifest 字段。Product 消费生成的运行时配置。公开验收证据只包含配置档、模式、组件状态、源码修订版、包版本、CUDA 信息以及二进制 / 协议摘要。

启动顺序为：NVIDIA adapter → sandboxd → Kernel → trainer probe → placement 和 serving probe → Reactor Host → Reactor control → 其余 Product。关闭时，先停止 Product 流量和部署，再停止 Reactor 进程，最后依次停止 Kernel → sandboxd → NVIDIA adapter。Pipeline teardown 在 `always()` 下运行。

```bash
export CYRENE_PLATFORM_WORKTREE=/opt/cyrene/Cyrene-Platform
export CYRENE_YIELD_WORKTREE=/opt/cyrene/Cyrene-Yield
export CYRENE_REACTOR_WORKTREE=/opt/cyrene/Cyrene-Reactor
export CYRENE_RUNTIME_HOME=/srv/cyrene/reference-runtime
scripts/reference-runtime bootstrap
scripts/reference-runtime status
```

self-hosted Agent 必须限制为受信任的 Pipeline，使用专用的最小权限服务账号和工作目录，并且只能通过 Pipeline secret 变量接收秘密。Fork pull request 不得进入 GPU 任务。
