# Cyrene Documentation

Cyrene 文档索引（Cyrene documentation index）。本目录记录跨仓库架构、产品边界、实施计划和可复现验收规则；各产品的领域契约、源代码和运行手册仍由各自仓库拥有。

## Plans / 计划

| Document | Purpose / 用途 | Status / 状态 |
| --- | --- | --- |
| [Cyrene Text Model Lifecycle V1](plans/cyrene-text-model-lifecycle-v1.md) | 文本模型从数据、训练、部署、网关、桌面使用到评估反馈的开放闭环，以及 Navigator adoption proof | Planning; no functional gate passed |

## Reading order / 推荐阅读顺序

1. 先阅读 Workspace 的 `README.md`、`repositories.yaml` 和 `governance/product-contract-v1/README.md`，了解仓库拓扑与当前 canonical authority。
2. 再阅读具体产品仓库的 Product contract 和运行时 README。
3. 最后按照计划文档的阶段、检查 ID 和证据记录推进实现。

计划文档使用中英双语标题或关键术语。正文以中文为主，英文说明只保留能帮助跨仓库协作的最小术语。
