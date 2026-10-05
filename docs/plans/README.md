# Plans / 实施计划

本目录保存跨产品、跨仓库的可执行实施计划。每份计划都必须说明范围、权威边界、阶段顺序、稳定检查 ID、验证环境和证据字段。

## Status rule / 状态规则

- `[ ]` 表示尚未获得实际验收证据；新计划中的功能项必须从 `[ ]` 开始。
- `[x]` 只表示该检查的 **Actual PASS** 已经在声明的真实环境完成，并且证据字段完整。
- `Code complete` 只说明实现已经落盘；`Local tests` 只说明目标仓库的局部测试通过；两者都不能替代 `Actual PASS`。
- 失败、阻塞、跳过和环境不足必须写进限制字段，不能通过取消测试、静默 fallback 或只运行 fake executor 来勾选。
- 检查 ID 一旦发布不得复用。需要拆分检查时保留旧 ID，并添加新的子 ID。

## Plans / 计划清单

| Plan | Scope / 范围 | Current status / 当前状态 |
| --- | --- | --- |
| [Navigator Work Assistant Round 1](navigator-work-assistant-round1.md) | Durable dsh work assistant, WeCom, QQ, native subagents, cloud patrols and paired Client | Implementing; real host acceptance and VM cutover separate |
| [Connection Components V2](connection-components-v2.md) | Product catalogs, connection boundaries, confirmed component updates and native two-host acceptance | Implementing; actual deployment gates pending |
| [Cyrene Text Model Lifecycle V1](cyrene-text-model-lifecycle-v1.md) | Text LLM、NVIDIA CUDA、LLaMA Factory SFT/LoRA、vLLM、Exchange、Navigator Harness、Echo 反馈和开放式资源交接 | Implementing; 30/32 scoped Phase 0 checks have Actual PASS evidence; P0-GATE and later gates pending |
| [Cyrene First Usable RC V1](cyrene-first-rc-v1.md) | 安装、浏览器控制台、模型导入、数据集、单卡 SFT + LoRA、vLLM 部署、Exchange 网关与 API Key、外部客户端 | Implementing; Wave 0 baseline and Wave 2 import/gateway API foundations have LOCAL_TEST_PASS; installer, WebUI, real GPU and HTTPS gates pending |
| [Cyrene RC Logging & Diagnostics](rc-logging-and-diagnostics.md) | 结构化日志、错误码命名空间、关联诊断、本地持久化与容灾预算，落实 logging-and-errors 规范 | Implementing; Wave 0 baseline and matrices complete; Wave 1-6 planned |

Current evidence / 当前证据： [Connection Components V2](connection-components-v2-evidence.md)；[Phase 0, 2026-09-06](evidence/2026-09-06-phase-0.md)；
[First RC Wave 0 / Wave 2, 2026-09-20](evidence/2026-09-20-rc-wave0-wave2.md)；
[RC Logging & Diagnostics, 2026-09-21](evidence/2026-09-21-rc-logging-diagnostics.md)。

Frozen client handoff / 固定客户端交接：[Navigator V8](handoff/cyrene-navigator-v8/README.md)。
---
<!-- Chinese Translation / 中文翻译 -->

# 计划 / 实施计划

本目录保存跨产品、跨仓库的可执行实施计划。每份计划都必须说明范围、权威边界、阶段顺序、稳定检查 ID、验证环境和证据字段。

## 状态规则

- `[ ]` 表示尚未获得实际验收证据；新计划中的功能项必须从 `[ ]` 开始。
- `[x]` 只表示该检查的 **Actual PASS** 已经在声明的真实环境完成，并且证据字段完整。
- `Code complete` 只说明实现已经落盘；`Local tests` 只说明目标仓库的局部测试通过；两者都不能替代 `Actual PASS`。
- 失败、阻塞、跳过和环境不足必须写入限制字段；不得通过取消测试、静默 fallback 或只运行 fake executor 来勾选。
- 检查 ID 一旦发布不得复用。需要拆分检查时，应保留旧 ID 并添加新的子 ID。

## 计划清单

| 计划 | 范围 | 当前状态 |
|---|---|---|
| [Navigator 工作助手第一轮](navigator-work-assistant-round1.md) | 持久 dsh 工作助手、企微、QQ、原生子代理、云巡检与配对 Client | 实施中；真实宿主验收和 VM 切换单独记录 |
| [连接组件 V2](connection-components-v2.md) | Product 操作目录、连接边界、确认后组件更新与原生双机验收 | 正在实施；真实部署门禁尚待验收 |
| [Cyrene Text Model Lifecycle V1](cyrene-text-model-lifecycle-v1.md) | 文本 LLM、NVIDIA CUDA、LLaMA Factory SFT/LoRA、vLLM、Exchange、Navigator Harness、Echo 反馈和开放式资源交接 | 正在实施；指定范围内 Phase 0 的 32 项检查中，30 项已有 Actual PASS 证据；P0-GATE 和后续门禁仍待完成 |
| [Cyrene First Usable RC V1](cyrene-first-rc-v1.md) | 安装、浏览器控制台、模型导入、数据集、单卡 SFT + LoRA、vLLM 部署、Exchange 网关与 API Key、外部客户端 | 正在实施；Wave 0 基线和 Wave 2 导入 / 网关 API 基础已取得 LOCAL_TEST_PASS；安装器、WebUI、真实 GPU 和 HTTPS 门禁仍待完成 |
| [Cyrene RC Logging & Diagnostics](rc-logging-and-diagnostics.md) | 结构化日志、错误码命名空间、关联诊断、本地持久化与容灾预算，落实 logging-and-errors 规范 | 正在实施；Wave 0 基线和矩阵已完成；Wave 1–6 已规划 |

当前证据：[连接组件 V2](connection-components-v2-evidence.md)；[Phase 0，2026-09-06](evidence/2026-09-06-phase-0.md)；[First RC Wave 0 / Wave 2，2026-09-20](evidence/2026-09-20-rc-wave0-wave2.md)；[RC Logging & Diagnostics，2026-09-21](evidence/2026-09-21-rc-logging-diagnostics.md)。

固定客户端交接：[Navigator V8](handoff/cyrene-navigator-v8/README.md)。
