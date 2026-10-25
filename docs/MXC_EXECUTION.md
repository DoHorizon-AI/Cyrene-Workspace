# Optional Microsoft MXC execution backend

MXC is an optional command backend for the existing Computer Runtime. Navigator/DSH remains the executor; Exchange still authorizes the four computer tools and calls the existing Plugin transport. MXC does not introduce an agent, queue, credential store, or alternative authorization service.

The ordinary `Dockerfile.computer-runtime` keeps the `managed` backend. The separate `tooling/deploy/teams-cloud/Dockerfile.computer-runtime-mxc` compiles the optional `mxc` feature and selects `CYRENE_COMPUTER_EXECUTION_BACKEND=mxc`. It pins the MIT-licensed Rust `mxc-sdk` to **1.0.1** through the Plugin dependency and lockfiles and packages **Bubblewrap 0.13.1**, whose official source archive is checked against SHA-256 `0e85c8702248b8dd64144f594147553650cdd0e4c951690345faace79c217c4d`.

## Boundary and failure behavior

Linux uses Bubblewrap process namespaces and a shared kernel, rather than a hardware VM. The host supplies the workspace roots; only those roots are writable in the sandbox. The SDK's minimal system baseline is read-only. The workload receives a filtered environment and a private home directory; provider credentials, the caller's home, Docker sockets, and control request/receipt files are not mounted. Egress, ingress, and host loopback access are denied. Connectors continue calling provider APIs from their existing credential-bearing services.

Unary and streaming commands use the same backend and bounded lifecycle. Timeouts, cancellation, output limits, full output hashes, and exit outcomes retain their existing meaning. A private start marker and terminal receipt distinguish a workload exit from a sandbox setup failure. Missing dependencies, unsupported namespaces, a missing helper, or failed probes cause an explicit error. Selecting `mxc` never silently executes a managed shell.

The backend requires Linux user namespaces, a usable rootless Bubblewrap installation, and **Bubblewrap 0.12.0 or newer**. The stricter version floor accounts for [the upstream setup-time symlink advisory](https://github.com/containers/bubblewrap/security/advisories/GHSA-pxhw-h44j-8pfx). The packaged version is 0.13.1. Startup runs an actual contained workload, rather than relying on the SDK's advisory availability result; see [MXC issue 1494](https://github.com/microsoft/mxc/issues/1494).

## Build and enable

1. Merge and pin the Plugin revision that contains the `mxc` Cargo feature, `cyrene-mxc-exec` helper, and both updated lockfiles. Pin the Workspace revision as usual.
2. In the private ARM64 build plan, change only the `computer-runtime` image's `dockerfile` to `Cyrene-Workspace/tooling/deploy/teams-cloud/Dockerfile.computer-runtime-mxc`. Keep the normal immutable Rust/runtime base image arguments and the existing `sourceRepos` entries. Build using `build_arm64.py`; record the actual image ID, source revisions, architecture, and footprint. This is an explicit alternative image, not a default-profile change.
3. Before enabling the image, run its helper under the exact intended container restrictions and workspace mounts. The helper is `/usr/local/bin/cyrene-mxc-exec --probe --workspace-root /workspace`; it runs a bounded real namespace probe and prints its fixed success marker only after verified completion. The server repeats the probe at startup. A successful `bwrap --version`, image build, or health-check binary alone is insufficient.
4. Use the existing computer-runtime Compose overlay, private mTLS proxy, non-root user, read-only root, dropped capabilities, `no-new-privileges`, resource caps, and bounded scratch tmpfs unchanged. Pass the regular deployment preflight. If containment is unavailable, keep the ordinary managed image until a separately reviewed host profile is available. Do not add `privileged`, `SYS_ADMIN`, an unconfined security profile, or host sysctl changes as an automatic workaround.

The image keeps the original Bubblewrap `COPYING` and complete, unmodified source archive in `/usr/share/doc/cyrene/`. Retain those alongside the source/image lock when redistributing the image.

## Acceptance limits

The native Linux x86_64/WSL2 tests are evidence for that host. ARM64 image builds and emulated execution are recorded separately from actual ARM64 host acceptance. Windows and macOS execution are not enabled by this profile. Ordinary Docker restrictions can deny the required user namespaces; such a rejection is an unavailable backend, never a successful command result.

The Teams cloud computer profile uses temporary scratch storage. Workspace files and scratch artifacts disappear when that container is replaced or restarted; Navigator's durable task/session records remain on their existing persistent volume. MXC does not add persistence or change the current four-tool catalog. Model connectivity, Teams delivery, recovery, and 24-hour service stability require their own acceptance.

References: [MXC 1.0.1 release](https://github.com/microsoft/mxc/releases/tag/v1.0.1), [Linux backend](https://github.com/microsoft/mxc/blob/v1.0.1/docs/backends/bwrap/bubblewrap-backend.md), [Bubblewrap 0.13.1 release](https://github.com/containers/bubblewrap/releases/tag/v0.13.1).

---
<!-- Chinese Translation / 中文翻译 -->

# 可选的 Microsoft MXC 执行后端

MXC 接入既有 Computer Runtime，负责命令执行隔离。Navigator/DSH 继续负责执行任务，Exchange 继续授权并调用现有四项电脑工具；不新增 agent、任务队列、凭据存储或授权服务。

普通 `Dockerfile.computer-runtime` 保持 `managed`。单独的 `Dockerfile.computer-runtime-mxc` 构建 `mxc` feature，并配置 `CYRENE_COMPUTER_EXECUTION_BACKEND=mxc`。Plugins 的依赖与锁文件固定 **MXC SDK 1.0.1**；镜像固定打包 **Bubblewrap 0.13.1**，并核对官方源码压缩包的完整 SHA-256。

Linux 使用共享内核的进程命名空间隔离。工作区由宿主配置，只有工作区可写；系统工具只读。工作负载只获得过滤后的环境和独立临时 HOME，不挂载供应商凭据、用户主目录、Docker socket 或控制文件。出站、入站和宿主回环网络均拒绝，供应商 API 仍由原有连接器服务调用。

普通与流式命令共用同一后端，保留超时、取消、输出上限、完整输出哈希和真实退出结果。私有启动标记及终态回执用于识别沙箱未启动的情况。选择 MXC 后，缺依赖、命名空间不可用、helper 缺失或探测失败都会明确报错，不自动退回普通 shell。Bubblewrap 至少要求 0.12.0，以覆盖官方已公布的启动阶段符号链接修复；镜像内实际固定为 0.13.1。

启用时，在私有 ARM64 构建计划中只将 `computer-runtime` 的 Dockerfile 改为上述可选文件，继续固定已合并源码、基础镜像、实际镜像摘要、架构和大小。先在目标容器的实际权限及挂载条件下运行 `cyrene-mxc-exec --probe --workspace-root /workspace`；服务启动时也会执行真实探测。安装了 Bubblewrap、构建成功或版本查询成功均不能替代该探测。

现有 Compose 的非 root、只读根目录、drop ALL、no-new-privileges、私有 mTLS、资源限制及临时存储规则不放宽。目标环境不支持时，继续使用普通 managed 镜像，等待单独评审的宿主配置；不自动增加特权、SYS_ADMIN、关闭安全配置或修改宿主 sysctl。镜像保留 Bubblewrap 原版许可证及完整源码包，随镜像锁一同保留。

本机 x86_64/WSL2 验收、ARM64 构建、模拟执行和 ARM64 真机验收分别记录。当前配置不启用 Windows/macOS。默认 Docker 拒绝用户命名空间时，必须记录为后端不可用。

Teams 云端电脑工作区及 artifact 为临时存储，容器替换或重启后不保留；Navigator 的任务和会话继续使用原有持久卷。MXC 不改变四项工具目录，也不替代模型连通、Teams 回传、恢复和 24 小时稳定性验收。
