# Cyrene Documentation Index

This directory contains the canonical, current-first documentation for the Cyrene AI ecosystem.

---

## 1. System Architecture & Products
- [**ARCHITECTURE.md**](ARCHITECTURE.md): Complete system architecture, 3-tier model, and platform boundaries.
- [**PRODUCTS.md**](PRODUCTS.md): Product catalog and service responsibilities (Catalyst, Yield, Reactor, Exchange, Navigator, Echo).
- [**RUNTIME.md**](RUNTIME.md): Clean-room runtime execution model and environment profiles.
- [**PLUGINS.md**](PLUGINS.md): Plugin architecture, `plugin.manifest.json` specification, and catalog governance.
- [**Exchange Integration v1 ADR**](ADR_EXCHANGE_INTEGRATION_V1.md): Accepted internal GitHub-to-WeCom architecture, ownership, contracts, compatibility, and implementation evidence gates.
- [**Authorization, Cloud Observation, and Recovery v1 ADR**](ADR_AUTHORIZATION_CLOUD_V1.md): Provider identity boundaries, signed health/renewal contracts, deterministic monitoring, durable recovery, deployment gates, and verified source, release, and acceptance evidence.
- [**Exchange Integration v1 Deployment & Acceptance**](EXCHANGE_INTEGRATION_V1_DEPLOYMENT.md): Installer modes, Platform Package Runtime selection, host configuration, and NOT_RUN native acceptance gates.
- [**Teams cloud deployment**](TEAMS_CLOUD_DEPLOYMENT.md): ARM64 image locks, isolated Teams ingress, Directory owner maintenance, staged providers, rollback, and bounded-cost acceptance.
- [**Optional MXC execution**](MXC_EXECUTION.md): Pinned command sandbox, real containment probes, unchanged executor ownership, and deployment limits.
- [**Modular distribution ADR**](ADR_MODULAR_DISTRIBUTION_V01.md): Ubuntu workload selection, signed releases, deployment boundaries, and installed-service acceptance gates.

---

## 2. Engineering & Developer Guides
- [**QUICKSTART.md**](QUICKSTART.md): Prerequisites, bootstrap, and developer onboarding.
- [**DEVELOPMENT.md**](DEVELOPMENT.md): Multi-repo topology, agent worktree isolation, JetBrains IDE integration, and quality gates.
- [**TEAMS_CHAT_PRIVACY.md**](TEAMS_CHAT_PRIVACY.md): Internal Teams assistant data processing, storage, and contact notice.
- [**TEAMS_CHAT_USAGE.md**](TEAMS_CHAT_USAGE.md): Internal Teams assistant usage and connector-operation guidance.
- [**TASK_LIFECYCLE.md**](TASK_LIFECYCLE.md): Cross-tool delivery requirements, guarded cleanup, and preserved unfinished work. 跨工具交付、安全清理与未完成任务保留规则。
- [**DATA_TOOLS_TRIAL.md**](DATA_TOOLS_TRIAL.md): Linux Catalyst/Echo trial launcher, optional UI, remote binding, backup, and cleanup.
- [**CATALYST_V02_WORKFLOW.md**](CATALYST_V02_WORKFLOW.md): Catalyst-only source review, isolated OCR setup, one-call local model acceptance, dual-profile publishing, and restart recovery.
- [**Component update packaging**](../packaging/service-bundle.md): Linux component updater, verified catalog metadata, and immutable service packaging.

---

## 3. Specifications & Governance
- [**Shared Runtime Directory Lifecycle ADR**](ADR_SHARED_RUNTIME_DIRECTORY_LIFECYCLE.md): systemd lifecycle contract for the shared `/run/cyrene` directory.
- [**TEXT_MODEL_LIFECYCLE.md**](TEXT_MODEL_LIFECYCLE.md): Six-stage closed loop from data engineering to evaluation feedback.
- [**COMPATIBILITY.md**](COMPATIBILITY.md): OpenAI API compatibility and scoped Plugin migration surfaces.
- [**MIGRATION.md**](MIGRATION.md): Legacy surface final closure report and verification inventory.
- [**CI Authority**](governance/ci-authority.md): GitHub Actions/Azure ownership, automatic trigger boundary, and evidence rules.
- [**Logging & Error Standards**](standards/logging-and-errors.md): Unified structured logging format, stable error code taxonomy, and diagnostic boundaries (v0.1 draft).

---

## 4. Plans & Evidence
- [**Plans**](plans/): Detailed implementation plans and milestone roadmaps.
- [**Text Model Lifecycle V1 Evidence**](text-model-lifecycle-v1-canonical-evidence.md): Canonical test evidence.
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene 文档索引

本目录收录 Cyrene AI 生态系统以当前内容为先的规范文档。

---

## 1. 系统架构与产品
- [**ARCHITECTURE.md**](ARCHITECTURE.md)：完整系统架构、三层模型和 Platform 边界。
- [**PRODUCTS.md**](PRODUCTS.md)：产品目录及服务职责（Catalyst、Yield、Reactor、Exchange、Navigator、Echo）。
- [**RUNTIME.md**](RUNTIME.md)：洁净室运行时执行模型和环境配置档。
- [**PLUGINS.md**](PLUGINS.md)：插件架构、`plugin.manifest.json` 规范及目录治理。
- [**Exchange Integration v1 ADR**](ADR_EXCHANGE_INTEGRATION_V1.md)：已接受的内部 GitHub 到企业微信架构、权属、合约、兼容性和实现证据门禁。
- [**Authorization, Cloud Observation, and Recovery v1 ADR**](ADR_AUTHORIZATION_CLOUD_V1.md)：Provider 身份边界、签名 health/renewal 合约、确定性监控、持久恢复、部署门禁，以及经核验的源码、发行物和验收证据。
- [**Exchange Integration v1 部署与验收**](EXCHANGE_INTEGRATION_V1_DEPLOYMENT.md)：安装模式、Platform Package Runtime 选择、主机配置和 NOT_RUN 原生验收门禁。
- [**Teams 云端部署**](TEAMS_CLOUD_DEPLOYMENT.md)：ARM64 镜像锁定、独立 Teams ingress、Directory owner 维护、分阶段 Provider、回滚和有界成本验收。
- [**可选 MXC 执行**](MXC_EXECUTION.md)：固定版本的命令沙箱、真实隔离探测、既有执行权属和部署限制。
- [**主动助手 v1 部署配置**](TEAMS_CLOUD_DEPLOYMENT.md)：默认模型、Teams 持续聊天、持久工作流、GitHub 主动观察和低权限电脑执行边界。
- [**模块化发行 ADR**](ADR_MODULAR_DISTRIBUTION_V01.md)：Ubuntu 工作负载选择、签名发布、部署边界与安装后验收门禁。

---

## 2. 工程与开发者指南
- [**QUICKSTART.md**](QUICKSTART.md)：前置条件、引导流程和开发者入门。
- [**DEVELOPMENT.md**](DEVELOPMENT.md)：多仓库拓扑、Agent 工作树隔离、JetBrains IDE 集成和质量门禁。
- [**TEAMS_CHAT_PRIVACY.md**](TEAMS_CHAT_PRIVACY.md)：Teams 助手处理信息、存储和联系渠道的内部说明。
- [**TEAMS_CHAT_USAGE.md**](TEAMS_CHAT_USAGE.md)：Teams 助手使用方式与连接器操作说明。
- [**DATA_TOOLS_TRIAL.md**](DATA_TOOLS_TRIAL.md)：Linux Catalyst/Echo 试用启动器、可选 UI、远程绑定、备份和清理。
- [**CATALYST_V02_WORKFLOW.md**](CATALYST_V02_WORKFLOW.md)：Catalyst-only 来源审核、隔离 OCR 配置、单次本地模型验收、双制品发布和重启恢复。
- [**Component update packaging**](../packaging/service-bundle.md)：Linux 组件更新器、已验证目录元数据和不可变服务打包。

---

## 3. 规范与治理
- [**共享运行时目录生命周期 ADR**](ADR_SHARED_RUNTIME_DIRECTORY_LIFECYCLE.md)：共享 `/run/cyrene` 目录的 systemd 生命周期合约。
- [**TEXT_MODEL_LIFECYCLE.md**](TEXT_MODEL_LIFECYCLE.md)：从数据工程到评估反馈的六阶段闭环。
- [**COMPATIBILITY.md**](COMPATIBILITY.md)：OpenAI API 兼容性及限定范围的插件迁移代码面。
- [**MIGRATION.md**](MIGRATION.md)：旧版代码面最终清理报告及验证清单。
- [**CI Authority**](governance/ci-authority.md)：GitHub Actions/Azure 的职责归属、自动触发边界和证据规则。
- [**Logging & Error Standards**](standards/logging-and-errors.md)：统一结构化日志格式、稳定错误码分类和诊断边界（v0.1 草案）。

---

## 4. 计划与证据
- [**Plans**](plans/)：详细实施计划和里程碑路线图。
- [**Text Model Lifecycle V1 Evidence**](text-model-lifecycle-v1-canonical-evidence.md)：规范测试证据。
