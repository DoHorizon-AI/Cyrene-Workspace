# ADR: Teams Cloud Plugin Processes

**Status:** Accepted on 2026-11-03 by the project owner. Applies to `tooling/deploy/teams-cloud` only.

## Context

Teams cloud runs each capability plugin in its own Compose service and reaches it over mTLS. We tested whether GitHub, Hugging Face, and Microsoft Graph should instead share one `cy-package-runtime` node, to reduce the container count. All three hold secrets.

The native distribution keeps using Package Runtime; see the [modular distribution ADR](ADR_MODULAR_DISTRIBUTION_V01.md). No earlier ADR required it for Teams cloud.

The tests ran on a development machine at Platform `afed688`, Plugins `1b93fe6`, and Exchange `12fdd89`, using locally built packages of `document-parsing` and Hugging Face. Account Center leases were fixtures, and only the last hop of an Exchange call ran.

## Decision

- Keep one container per plugin, managed by Compose. Do not merge plugins into a shared Package Runtime.
- Keep the plugin package as the unit of release. Build images from official packages once those are published.
- Keep mTLS between Exchange and plugins. Do not make Unix sockets the default: a plugin could place a symlink in its own socket volume and redirect a caller to another plugin.
- Add read-only root filesystems, dropped capabilities, and `no-new-privileges` where they are still missing. Microsoft Graph already has them.

## Why

| Measured | Shared Package Runtime | One container per plugin |
| --- | --- | --- |
| Read another plugin's environment, or kill it | Possible; all workers share one UID | Refused; each sees only its own processes |
| Change dependency code that runs on next start | Possible and undetected; `verify_installed_integrity` does not hash the dependency tree | Refused in the hardened test containers, where root owns the files. Hugging Face and GitHub are not hardened yet |
| Plugin crash | No automatic restart; an owner process must recover it and supply its secrets again | Restart policy recovers it: about 1.2 s for `document-parsing`, no CPU quota |
| Update one plugin | Every plugin stops: about 4 s with one plugin and no CPU quota, including about 1.3 s of test script wait | Only that plugin stops: about 1 s for `document-parsing` with no CPU quota, once 2.48 s |
| Hugging Face start at 0.1 CPU | 2.11 s, with the quota on the whole container | 5.2 to 5.4 s at its production 0.10 quota |
| New version crashes on start | Old version restored automatically in 0.48 s | Restart loop under plain `compose up`; the update engine documents rollback, not exercised here |

The container count was the reason to merge, and by memory it saves little. Merging three plugins removes two container shims, about 10 MiB measured. It adds Package Runtime and the Broker, about 9 MiB measured, plus an owner process that was not measured. This is an estimate. It does not count CPU: quotas drive cold start, and they have to be relaxed on either path.

## Consequences

- The container count does not fall. Each new platform still adds one Compose service, and today Hugging Face appears in 37 files under `tooling/deploy/teams-cloud`, with its own networks and keys.
- The official packages this needs are not published yet: Hugging Face, Graph, Teams, and Cloud Management have no release, and the latest 30 releases have no ARM64 assets.
- The image built in the test cannot replace a production image. It lacks the `--tls-*` arguments, `cyrene-secret-env-entrypoint`, and the mTLS health probe. The package also lacks the `hf` CLI that the production Hugging Face image carries.
- Hardening changes Compose configuration, which `runtime_update.py` does not allow in an image update. It needs its own deployment step.
- Each update ships a whole image, and images use more disk than packages that share dependencies.
- mTLS certificates need upkeep. We did not find expiry or rotation tooling for them.

## Alternatives

- **Package Runtime with one UID per plugin.** A 67-line experimental patch refused the 13 direct attacks we listed, but the daemon then needs four capabilities. Shared `/tmp`, `/dev/shm`, abstract sockets, and a world-writable lock file stay open, and with the shared staging directory the test used, one plugin could read another's input and rename its result. Every plugin still stops during an install, and the owner still holds every secret.
- **Package Runtime with a slimmer Broker.** Not tested. It addresses neither isolation nor the shared stop.

Revisit this if the container count starts to matter more than isolation, or if Teams cloud must share one mechanism with native products.

## Not verified

Real ARM64 hardware (ARM64 results come from QEMU, for Hugging Face and its health check only), the deployed update engine, the full Exchange service path, a real tool call after Hugging Face recovers from a crash, and GitHub, Graph, or Teams running from packages.

---
<!-- Chinese Translation / 中文翻译 -->

# ADR：Teams 云端插件进程

**状态：** 项目负责人于 2026-11-03 接受。只适用于 `tooling/deploy/teams-cloud`。

## 背景

Teams 云端让每个能力插件运行在各自的 Compose 服务里，通过 mTLS 访问。我们测试了能否让 GitHub、Hugging Face 和 Microsoft Graph 共用一个 `cy-package-runtime` 节点，以减少容器数。这三个插件都持有密钥。

原生发行继续使用 Package Runtime，见[模块化发行 ADR](ADR_MODULAR_DISTRIBUTION_V01.md)。此前没有 ADR 要求 Teams 云端使用它。

测试在开发机上进行，版本为 Platform `afed688`、Plugins `1b93fe6`、Exchange `12fdd89`，使用本地构建的 `document-parsing` 和 Hugging Face 包。Account Center 租约是夹具，Exchange 调用只运行了最后一跳。

## 决策

- 每个插件继续使用一个容器，由 Compose 管理。不把插件合并到共享的 Package Runtime。
- 插件包仍是发布单位。官方包发布后，从官方包构建镜像。
- Exchange 和插件之间继续使用 mTLS。不把 Unix socket 作为默认：插件可以在自己的 socket 卷里放符号链接，把调用方转到另一个插件。
- 给还缺少的插件加上只读根文件系统、移除能力和 `no-new-privileges`。Microsoft Graph 已经配置。

## 原因

| 实测项 | 共享 Package Runtime | 每个插件一个容器 |
| --- | --- | --- |
| 读取另一个插件的环境变量或杀掉它 | 可以，所有 worker 共用一个 UID | 被拒绝，只能看到自己的进程 |
| 修改下次启动时运行的依赖代码 | 可以且不会被发现，`verify_installed_integrity` 不校验依赖目录 | 在加固过的测试容器里被拒绝，文件属于 root。Hugging Face 和 GitHub 还没有加固 |
| 插件崩溃 | 不自动重启，负责方进程要恢复它并重新提供密钥 | 重启策略恢复：`document-parsing` 无 CPU 配额时约 1.2 秒 |
| 更新一个插件 | 所有插件都停：只有一个插件且无 CPU 配额时约 4 秒，含测试脚本等待约 1.3 秒 | 只停这个插件：`document-parsing` 无 CPU 配额时约 1 秒，有一次 2.48 秒 |
| Hugging Face 在 0.1 CPU 下启动 | 2.11 秒，配额加在整个容器上 | 5.2 到 5.4 秒，线上配额就是 0.10 |
| 新版本启动就崩溃 | 0.48 秒内自动恢复旧版 | 裸 `compose up` 下循环重启；更新引擎的文档写了回滚，这里没有运行 |

合并的理由是减少容器数，但按内存算省得很少。合并三个插件减少两个容器 shim，实测约 10 MiB；同时增加 Package Runtime 和 Broker，实测约 9 MiB，还要加一个没有实测的负责方进程。这是估算。这里没有算 CPU：冷启动主要受配额影响，两条路都要放宽配额。

## 后果

- 容器数不会下降。每个新平台仍要增加一个 Compose 服务，目前 Hugging Face 出现在 `tooling/deploy/teams-cloud` 下的 37 个文件里，还有专门的网络和密钥。
- 这个决策需要的官方包还没有发布：Hugging Face、Graph、Teams 和 Cloud Management 没有 release，最近 30 个 release 没有 ARM64 资产。
- 测试中构建的镜像不能替换线上镜像，它缺少 `--tls-*` 参数、`cyrene-secret-env-entrypoint` 和 mTLS 健康探针。官方包也没有线上 Hugging Face 镜像带的 `hf` CLI。
- 加固要改 Compose 配置，而 `runtime_update.py` 的镜像更新不允许这种改动，需要单独的部署步骤。
- 每次更新都传输整个镜像，镜像也比共享依赖的包更占磁盘。
- mTLS 证书需要维护。我们没有找到针对它们的到期或轮换工具。

## 其他方案

- **Package Runtime 加每插件一个 UID。** 一个 67 行的实验补丁拒绝了我们列出的 13 项直接攻击，但守护进程因此需要四个能力。共享的 `/tmp`、`/dev/shm`、抽象 socket 和一个所有人可写的锁文件仍然开放；在测试用的共享 staging 目录下，一个插件还能读另一个插件的输入并改其结果的文件名。安装时所有插件依然要停，负责方依然持有全部密钥。
- **精简 Broker 的 Package Runtime。** 没有测试。它既不解决隔离，也不解决一起停。

如果容器数变得比隔离更重要，或者 Teams 云端必须和原生产品共用一套机制，再重新评估。

## 没有验证

真实 ARM64 硬件（ARM64 结果来自 QEMU，只覆盖 Hugging Face 及其健康检查）、线上更新引擎、完整的 Exchange 服务路径、Hugging Face 崩溃恢复后的真实工具调用，以及以包形式运行的 GitHub、Graph 和 Teams。
