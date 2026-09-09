# Direct Product-to-Plugin vertical acceptance / Product 直连 Plugin 纵向验收

This directory contains a thin cross-repository orchestrator. It checks three immutable
commits and delegates capability behavior to the canonical owners:

- Cyrene-Plugins-Official builds the versioned OneBot packages;
- Cyrene-Platform builds only the generic package resolver and runtime;
- Astrbot-Rev runs its own `OfficialOneBotProductRuntimeTck` against two isolated bindings.

本目录只保留轻量的跨仓库编排。Plugins 构建版本化 OneBot 包，Platform 只构建通用包解析器与
运行时，Astrbot-Rev 用自己维护的 `OfficialOneBotProductRuntimeTck` 验证两个隔离 binding。

The TCK installs, starts, recovers, upgrades, rolls back, removes, and restores the package.
Inbound and outbound business messages travel directly between AstrBot and the Plugin process.
Platform never receives a message payload and does not own a capability-specific test driver.

TCK 覆盖安装、启动、恢复、升级、回滚、删除与离线恢复。入站和出站业务消息由 AstrBot 与 Plugin
进程直接传递；Platform 不接收消息 payload，也不维护能力专用测试驱动。

Run on Linux with the pinned defaults:

```bash
./ci/vertical-e2e/run.sh
```

Azure Pipelines supplies exact multi-checkout roots. A standalone run clones the same exact
commits into a disposable directory. Candidate revisions can be overridden explicitly:

Azure Pipelines is the CI authority for this acceptance. The obsolete GitHub Actions Phase 7
workflow was removed together with the CES-based driver.

本验收以 Azure Pipelines 为 CI 权威；旧 GitHub Actions Phase 7 工作流已随 CES 驱动一并删除。

```bash
CYRENE_PLATFORM_REF=<commit> \
CYRENE_PLUGINS_REF=<commit> \
CYRENE_ASTRBOT_REF=<commit> \
./ci/vertical-e2e/run.sh
```

成功时临时目录会被删除；失败时会输出 `DIRECT_PLUGIN_E2E_EVIDENCE_ROOT` 并保留现场。
