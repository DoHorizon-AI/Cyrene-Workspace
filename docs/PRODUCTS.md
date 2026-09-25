# Cyrene Product Services Catalog

Cyrene organizes functional capabilities into specialized, decoupled product repositories. Each product maintains its own clean-room test suite, independent CI pipeline, and service contract.

---

## 1. Cyrene-Catalyst (Data Engineering & Curation)
- **Repo**: `DoHorizon-AI/Cyrene-Catalyst`
- **Primary Responsibility**:
  - Owns dataset intake, mapping intent, review, lineage, and publication workflows.
  - Calls Plugins-owned preparation implementations for parsing, normalization, deduplication, splitting, and conversion.
  - Generates immutable `DatasetVersion` artifacts.
  - Enforces dataset provenance, licensing compliance, and minimal representative samples in public repositories.
- **Key Schemas / Artifacts**:
  - `DatasetVersion` (`dataset-version://...`)

---

## 2. Cyrene-Yield (Training & Fine-Tuning Engine)
- **Repo**: `DoHorizon-AI/Cyrene-Yield`
- **Primary Responsibility**:
  - Owns training drafts, runs, attempts, retry/cancel policy, checkpoints, results, and handoffs.
  - Calls Plugins-owned trainers and reusable dataset/model preflight capabilities through resolved endpoints.
  - Consumes `DatasetVersion` from Catalyst.
  - **Single Authority** for creating, hashing, and persisting canonical `ModelVersion` artifacts (`model-version://sha256/<hash>`).
- **Key Schemas / Artifacts**:
  - `ModelVersion`
  - `TrainingRun`

---

## 3. Cyrene-Reactor (Inference Serving Engine)
- **Repo**: `DoHorizon-AI/Cyrene-Reactor`
- **Primary Responsibility**:
  - Owns serving deployments, endpoint readiness, request admission, streaming, drain, and model-residency policy.
  - Calls Plugins-owned optimized execution engines; the retained Rust component is a thin Platform host-placement adapter.
  - Model deployment selection based on verified `ModelVersion` manifests.
- **Key Interfaces**:
  - OpenAI-compatible `/v1/chat/completions` and `/v1/completions`.
  - Internal high-speed IPC/gRPC execution endpoints.

---

## 4. Cyrene-Exchange (API Gateway & Governance)
- **Repo**: `DoHorizon-AI/Cyrene-Exchange`
- **Primary Responsibility**:
  - Unified entry point for API traffic, client routing, and policy enforcement.
  - Multi-tenant API key, quota, usage, route/fallback, and audit authority; invoicing and payments remain outside the Product.
  - Load balancing across multiple Reactor serving instances.
- **Domain Modules**:
  - `cyrene_exchange.governance` (`APIKey`, `TenantQuota`, `UsageRecord`, `BillingRecord`, `AuditLog`).

---

## 5. Cyrene-Navigator (Desktop Orchestrator & Client UI)
- **Repo**: `DoHorizon-AI/Cyrene-Navigator`
- **Primary Responsibility**:
  - Front-end user interface and desktop integration.
  - Local session orchestration and capability dispatch.
  - Connects to Exchange for inference and Echo for feedback submission.

---

## 6. Cyrene-Echo (Feedback, Evaluation & Analytics)
- **Repo**: `DoHorizon-AI/Cyrene-Echo`
- **Primary Responsibility**:
  - Collects user ratings, corrections, and execution feedback from Navigator sessions.
  - Owns evaluation suites, runs, quality gates, annotations, and feedback-set lifecycle.
  - Calls Plugins-owned evaluator implementations to compute reusable measurements.
  - Stores canonical analytical models (`SessionTokenUsage`, `SessionCost`, `ContextBreakdown`, `CapabilityDowngradeDisplay`).
  - Exports validated insights to Catalyst for dataset reinforcement loops.
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene 产品服务目录

Cyrene 将功能能力组织为专业化、解耦的产品仓库。每个产品都有自己的洁净室测试套件、独立 CI 流水线和服务合约。

---

## 1. Cyrene-Catalyst（数据工程与整理）
- **仓库**：`DoHorizon-AI/Cyrene-Catalyst`
- **主要职责**：
  - 负责数据集接入、映射意图、审核、血缘和发布工作流。
  - 调用由 Plugins 负责的准备能力实现，完成解析、规范化、去重、切分和格式转换。
  - 生成不可变的 `DatasetVersion` 制品。
  - 执行数据来源追踪和许可合规，并在公开仓库中只保留最小的代表性样本。
- **主要 Schema / 制品**：
  - `DatasetVersion`（`dataset-version://...`）

---

## 2. Cyrene-Yield（训练与微调引擎）
- **仓库**：`DoHorizon-AI/Cyrene-Yield`
- **主要职责**：
  - 负责训练草案、运行、尝试、重试 / 取消策略、检查点、结果和交接。
  - 通过已解析的端点调用由 Plugins 负责的训练器，以及可复用的数据集 / 模型预检能力。
  - 消费 Catalyst 提供的 `DatasetVersion`。
  - 是创建、哈希并持久化规范 `ModelVersion` 制品（`model-version://sha256/<hash>`）的**唯一权威**。
- **主要 Schema / 制品**：
  - `ModelVersion`
  - `TrainingRun`

---

## 3. Cyrene-Reactor（推理服务引擎）
- **仓库**：`DoHorizon-AI/Cyrene-Reactor`
- **主要职责**：
  - 负责服务部署、端点就绪状态、请求准入、流式响应、排空以及模型驻留策略。
  - 调用由 Plugins 负责的优化执行引擎；保留的 Rust 组件只是轻量级 Platform 主机放置适配器。
  - 根据经过验证的 `ModelVersion` 清单选择部署模型。
- **主要接口**：
  - 兼容 OpenAI 的 `/v1/chat/completions` 和 `/v1/completions`。
  - 内部高速 IPC/gRPC 执行端点。

---

## 4. Cyrene-Exchange（API 网关与治理）
- **仓库**：`DoHorizon-AI/Cyrene-Exchange`
- **主要职责**：
  - 为 API 流量、客户端路由和策略执行提供统一入口。
  - 负责多租户 API 密钥、配额、用量、路由 / 回退和审计；开票与支付仍在 Product 范围之外。
  - 在多个 Reactor 服务实例之间执行负载均衡。
- **领域模块**：
  - `cyrene_exchange.governance`（`APIKey`、`TenantQuota`、`UsageRecord`、`BillingRecord`、`AuditLog`）。

---

## 5. Cyrene-Navigator（桌面编排器与客户端 UI）
- **仓库**：`DoHorizon-AI/Cyrene-Navigator`
- **主要职责**：
  - 提供前端用户界面和桌面集成。
  - 编排本地会话并分发能力调用。
  - 连接 Exchange 进行推理，并连接 Echo 提交反馈。

---

## 6. Cyrene-Echo（反馈、评估与分析）
- **仓库**：`DoHorizon-AI/Cyrene-Echo`
- **主要职责**：
  - 收集用户对 Navigator 会话的评分、修正和执行反馈。
  - 负责评估套件、运行、质量门禁、标注和反馈集生命周期。
  - 调用由 Plugins 负责的评估器实现，计算可复用的测量指标。
  - 保存规范分析模型（`SessionTokenUsage`、`SessionCost`、`ContextBreakdown`、`CapabilityDowngradeDisplay`）。
  - 将经过验证的洞察导出至 Catalyst，供数据集强化闭环使用。
