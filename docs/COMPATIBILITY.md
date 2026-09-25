# Cyrene Compatibility Architecture & Boundary Standards

## 1. Compatibility Posture
Cyrene maintains a clear distinction between **Permanent Canonical Interfaces** and **Migration/Frozen Compatibility Surfaces**.

### A. Permanent Canonical Interfaces
- **OpenAI Compatible HTTP APIs**:
  - `Cyrene-Reactor` exposes `/v1/chat/completions`, `/v1/completions`, and `/v1/models`.
  - Adheres strictly to standard OpenAI request/response schemas.

### B. Migration & Compatibility Boundaries
Compatibility code remains explicitly scoped and must not become a second canonical runtime:

Under operator decision, `plugins/**/compatibility/` directories in `Cyrene-Plugins-Official` are temporarily retained as approved deferred exceptions while legacy third-party plugins migrate to `plugin.manifest.json`.
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene 兼容性架构与边界标准

## 1. 兼容策略
Cyrene 明确区分**永久规范接口**与**迁移期 / 冻结的兼容面**。

### A. 永久规范接口
- **兼容 OpenAI 的 HTTP API**：
  - `Cyrene-Reactor` 提供 `/v1/chat/completions`、`/v1/completions` 和 `/v1/models`。
  - 严格遵循 OpenAI 标准请求与响应 schema。

### B. 迁移与兼容边界
兼容代码必须明确限定范围，不得演变为第二套规范运行时：

根据运营方决策，在旧版第三方插件迁移至 `plugin.manifest.json` 期间，`Cyrene-Plugins-Official` 中的 `plugins/**/compatibility/` 目录将作为经批准、延后处理的例外暂时保留。
