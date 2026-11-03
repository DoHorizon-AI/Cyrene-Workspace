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
| Change dependency code that runs on next start | Possible and undetected; `verify_installed_integrity` does not hash the dependency tree | Refused by the read-only filesystem |
| Plugin crash | No automatic restart; an owner process must recover it and supply its secrets again | Restart policy recovers it in about 1.2 s |
| Update one plugin | Every plugin stops, about 4 s with one plugin and no CPU quota | Only that plugin stops: about 1 s for `document-parsing`, 5.2 to 5.4 s for Hugging Face at its 0.10 CPU quota |
| New version crashes on start | Old version restored automatically in 0.48 s | Restart loop under plain `compose up`; the update engine documents rollback, not exercised here |

The container count was the reason to merge, and it saves little. Merging three plugins removes two container shims, about 10 MiB measured. It adds Package Runtime and the Broker, about 9 MiB measured, plus an owner process that was not measured. This is an estimate.

## Consequences

- The container count does not fall, and each new platform still adds one Compose service.
- The official packages this needs are not published yet: Hugging Face, Graph, and Teams have no release, and the latest 30 releases have no ARM64 assets.
- The image built in the test cannot replace a production image. It lacks the `--tls-*` arguments, `cyrene-secret-env-entrypoint`, and the mTLS health probe.
- Hardening changes Compose configuration, which `runtime_update.py` does not allow in an image update. It needs its own deployment step.
- Each update ships a whole image, and images use more disk than packages that share dependencies.
- mTLS certificates need upkeep. We did not find expiry or rotation tooling for them.

## Alternatives

- **Package Runtime with one UID per plugin.** A 67-line experimental patch refused every direct attack we tried, but the daemon then needs four capabilities, and shared `/tmp`, `/dev/shm`, and abstract sockets remain open. Every plugin still stops during an install, and the owner still holds every secret.
- **Package Runtime with a slimmer Broker.** Not tested. It addresses neither isolation nor the shared stop.

Revisit this if the container count starts to matter more than isolation, or if Teams cloud must share one mechanism with native products.

## Not verified

Real ARM64 hardware (ARM64 results come from QEMU, for Hugging Face and its health check only), the deployed update engine, the full Exchange service path, and GitHub, Graph, or Teams running from packages.

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
| 修改下次启动时运行的依赖代码 | 可以且不会被发现，`verify_installed_integrity` 不校验依赖目录 | 被只读文件系统拒绝 |
| 插件崩溃 | 不自动重启，负责方进程要恢复它并重新提供密钥 | 重启策略约 1.2 秒恢复 |
| 更新一个插件 | 所有插件都停，只有一个插件且无 CPU 配额时约 4 秒 | 只停这个插件：`document-parsing` 约 1 秒，Hugging Face 在 0.10 CPU 配额下 5.2 到 5.4 秒 |
| 新版本启动就崩溃 | 0.48 秒内自动恢复旧版 | 裸 `compose up` 下循环重启；更新引擎的文档写了回滚，这里没有运行 |

合并的理由是减少容器数，但省得很少。合并三个插件减少两个容器 shim，实测约 10 MiB；同时增加 Package Runtime 和 Broker，实测约 9 MiB，还要加一个没有实测的负责方进程。这是估算。

## 后果

- 容器数不会下降，每个新平台仍要增加一个 Compose 服务。
- 这个决策需要的官方包还没有发布：Hugging Face、Graph 和 Teams 没有 release，最近 30 个 release 没有 ARM64 资产。
- 测试中构建的镜像不能替换线上镜像，它缺少 `--tls-*` 参数、`cyrene-secret-env-entrypoint` 和 mTLS 健康探针。
- 加固要改 Compose 配置，而 `runtime_update.py` 的镜像更新不允许这种改动，需要单独的部署步骤。
- 每次更新都传输整个镜像，镜像也比共享依赖的包更占磁盘。
- mTLS 证书需要维护。我们没有找到针对它们的到期或轮换工具。

## 其他方案

- **Package Runtime 加每插件一个 UID。** 一个 67 行的实验补丁拒绝了我们尝试的每一项直接攻击，但守护进程因此需要四个能力，共享的 `/tmp`、`/dev/shm` 和抽象 socket 仍然开放。安装时所有插件依然要停，负责方依然持有全部密钥。
- **精简 Broker 的 Package Runtime。** 没有测试。它既不解决隔离，也不解决一起停。

如果容器数变得比隔离更重要，或者 Teams 云端必须和原生产品共用一套机制，再重新评估。

## 没有验证

真实 ARM64 硬件（ARM64 结果来自 QEMU，只覆盖 Hugging Face 及其健康检查）、线上更新引擎、完整的 Exchange 服务路径，以及以包形式运行的 GitHub、Graph 和 Teams。
