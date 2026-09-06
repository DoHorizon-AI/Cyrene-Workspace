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
| [Cyrene Text Model Lifecycle V1](cyrene-text-model-lifecycle-v1.md) | Text LLM、NVIDIA CUDA、LLaMA Factory SFT/LoRA、vLLM、Exchange、Navigator Harness、Echo 反馈和开放式资源交接 | Planning; all functional checks unchecked |
