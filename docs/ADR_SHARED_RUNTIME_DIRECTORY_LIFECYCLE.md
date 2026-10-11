# ADR: Shared Runtime Directory Lifecycle

**Status:** Accepted; the Platform implementation is tracked in [Cyrene-Platform PR #104](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/104).

## Context

The native Platform services use `/run/cyrene` as a shared systemd runtime directory for Unix-domain sockets. Several units can declare the same directory. If systemd removes that shared directory when one unit stops, sockets owned by still-running sibling processes disappear even though those processes remain active. Restarting one service must not silently disconnect another service.

## Decision

- Every Platform unit that declares the shared `/run/cyrene` runtime directory sets `RuntimeDirectoryPreserve=yes`. Stopping or restarting one such unit must leave the shared directory and sibling sockets available.
- Each service process remains responsible for creating and removing its own socket. The shared-directory setting does not transfer socket ownership or change socket authentication.
- `/run` remains a runtime filesystem. The directory is recreated during service startup and is cleared by the normal host reboot lifecycle; preservation applies to unit stops, not across reboot.
- Keep the existing runtime-directory path, UID/GID, socket paths, peer-credential checks, package signatures, and maintenance-lock behavior unchanged. This decision changes only the shared directory's stop-time cleanup behavior.

## Validation and limits

An isolated systemd 255 reproduction used two transient services and a separate shared runtime directory. Service A held a Unix-domain socket. With `RuntimeDirectoryPreserve=yes`, stopping, restarting, and stopping service B left A's PID unchanged and its socket responsive. In a control run without preservation, stopping B removed the shared path while A remained active. The reproduction did not operate the real Cyrene units or a guest installation. It validates the directory-lifecycle behavior only; full installation, product, and clean-host acceptance require separate evidence.

The Platform change is scoped to the five units that share this runtime directory: Kernel, Broker, Linux System Adapter, NVIDIA Adapter, and sandboxd. It does not change their identities or runtime contracts.

---
<!-- Chinese Translation / 中文翻译 -->

# ADR：共享运行时目录生命周期

**状态：** 已接受；Platform 实现由 [Cyrene-Platform PR #104](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/104) 跟踪。

## 背景

原生 Platform 服务将 `/run/cyrene` 用作共享的 systemd 运行时目录，保存 Unix domain socket。多个 unit 可以声明同一个目录。如果某个 unit 停止时 systemd 删除了共享目录，仍在运行的同伴进程所持有的 socket 也会消失，尽管这些进程本身仍处于活动状态。重启一个服务不应意外断开另一个服务。

## 决策

- 每个声明共享 `/run/cyrene` 运行时目录的 Platform unit 都设置 `RuntimeDirectoryPreserve=yes`。停止或重启其中一个 unit 时，共享目录及同伴 socket 必须继续可用。
- 每个服务进程仍负责创建和移除自己的 socket。共享目录设置不会转移 socket 所有权，也不会改变 socket 身份验证。
- `/run` 仍是运行时文件系统。服务启动时会重新创建目录，主机正常重启时会清理该目录；保留规则仅适用于停止 unit，不跨越主机重启。
- 保持现有运行时目录路径、UID/GID、socket 路径、peer credential 检查、包签名和维护锁行为不变。本决策只改变停止 unit 时对共享目录的清理方式。

## 验证与限制

使用 systemd 255 完成了一项隔离复现：两个临时服务共用一个独立运行时目录，服务 A 持有 Unix domain socket。设置 `RuntimeDirectoryPreserve=yes` 后，停止、重启再停止服务 B，服务 A 的 PID 保持不变且 socket 仍可响应。对照运行未设置保留时，停止 B 会删除共享路径，而 A 仍处于活动状态。复现没有操作真实 Cyrene unit 或 guest 安装，只验证目录生命周期行为；完整安装、产品和干净主机验收需要单独证据。

Platform 改动仅涉及共用此运行时目录的五个 unit：Kernel、Broker、Linux System Adapter、NVIDIA Adapter 和 sandboxd，不改变其身份或运行时合约。
