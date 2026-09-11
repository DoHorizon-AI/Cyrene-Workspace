# Cyrene 全仓疑似老代码与规范偏差审计

> **Historical snapshot (2026-09-07).** This audit reflects the topology at that date, when
> `Astrbot-Rev` and `DH-System-Internal` were still canonical workspace members. Both
> repositories have since been retired; their migrated code lives under
> `Cyrene-Plugins-Official/plugins/`. The authoritative current inventory is `repositories.yaml`.

**Audit date / 审计日期**：2026-09-07
**First-pass target / 首轮目标**：`DoHorizon-AI/Cyrene-Plugins-Official`，`develop@0b13720e76184dc122e91726a0168ac12fc7030d`
**Expanded scope / 扩展范围**：Workspace `repositories.yaml` 中的 10 个成员仓库，加上 Workspace 自身，共 11 个仓库
**Report status / 报告状态**：源码只读审计；本报告未修改任何被审计仓库代码，也不构成能力发布、真实硬件验收或生产验收。

> **English abstract.** The Plugins `develop` tree no longer contains unregistered
> root-level `archive/` or `legacy/` directories, old manifest authorities, or
> tracked build/cache artifacts. Across the full Cyrene topology, however, active
> legacy aliases, simulated-success implementations, misplaced Product authority,
> explicitly runnable compatibility runtimes, mock-only product shells, stale
> deleted-path references, and repository-governance drift remain. Azure Hosted CI
> establishes the first integrated baseline for most current heads; it does not make
> skipped, simulated, credential-gated, real-GPU, or unqueued exact-head work pass.
> Sections 1–12 preserve the detailed Plugins audit. Sections 13 onward are the
> expanded cross-repository inventory and cleanup gate for the next phase.

## 1. Plugins 结论

当前 `develop` 已完成“表面遗留物清理”：没有跟踪中的根级 `archive/`、`legacy/`、`plugin.legacy.toml`、`LEGACY_PLUGIN.md`、缓存、编译产物或空文件。真正的问题集中在下面四类：

1. **能力状态失真**：7 个 `COMPATIBILITY_SHIM` 都不能通过 manifest 声明的入口独立加载，但 Catalog Set 仍把其中 6 项能力标成 `RESOLVED`。
2. **原型路径被写成已交付能力**：TensorRT 缺库时自动成功进入 mock、Seedance 默认返回样例视频且 wheel 为空、Dataset Validator 宣称支持 CSV 但实际拒绝、ASP.NET 网关仅有 health 与统一 `503` 骨架。
3. **边界债务仍很大**：11 组已登记兼容路径合计 559 个源码文件、169,552 行；Spring Gateway 仍是含租户、计费、训练、部署语义的 Product 快照。
4. **验证口径不完整**：默认 `pytest` 只运行 `conformance/tests`；主 CI 未覆盖 Spring、.NET、Agent、Skills、新增插件与 Ruff；Evidence Validator 只校验证据字符串，看不到测试是否存在或执行成功。

因此，“仓库已无老代码”目前只能解释为“旧 manifest 和 archive 外壳已删”。不能解释为“兼容实现已迁完”“22 个插件都能安装运行”或“全仓测试通过”。

## 2. 审计范围与方法

本轮检查了当前提交的全部 1,029 个跟踪文件，包括 292 个 Python、337 个 C#、88 个 Kotlin、2 个 Java 和 208 个 Markdown 文件，以及所有 manifest、catalog、lifecycle、CI、构建和治理文件。

检查方法包括：

- 远端 `develop`、`main`、仓库可见性、默认分支、GitHub Actions 和 Azure Pipeline 实时回读；
- 22 份 `plugin.manifest.json` 与 Catalog、Lifecycle、Boundary Registry 的集合和字段比对；
- Python 入口模块/符号、独立包构建、测试收集、Ruff 与格式检查；
- C# 项目、Spring/Gradle、Wrapper、测试和编译告警检查；
- TODO、未实现、占位、弃用、静默异常、模拟回退、兼容路径和重复大文件扫描；
- 与 Workspace 现有 Plugins 交付报告和治理文档交叉核对。

以下内容不按“发现关键字即认定缺陷”处理：抽象基类中的 `NotImplementedError`、测试 mock、协议生成代码、已明确登记的兼容树会单独分类。

## 3. 需要先处理的高优先级问题

### P0-1：7 个兼容 Shim 均不能通过声明入口独立加载

| Plugin | Manifest 入口 | 当前证据 |
| --- | --- | --- |
| `cyrene.agents.agent-system` | `astrbot.core.agent.runner:AgentRunner` | `runner` 模块不存在；测试收集又因缺少 `astrbot.core.provider` 失败。 |
| `cyrene.agents.skills-runtime` | `astrbot.core.skills:SkillManager` | 模块存在，但 `skill_manager.py` 导入未打包的 `astrbot.core.utils`；3 个测试文件收集失败。 |
| `cyrene.connectors.im` | `im_connector:ImConnector` | 模块不存在；Boundary Registry 明确记录 `NONE_TRACKED`。 |
| `cyrene.gateway.python` | `main:app` | `main.py` 不存在；实际保留的是 `python_gateway_lite.py` 和兼容 worker。 |
| `cyrene.policy.content-safety` | `content_policy:ContentPolicyEvaluator` | 模块不存在；无跟踪测试。 |
| `cyrene.policy.model-routing` | `model_api_router:ModelApiRouter` | 模块不存在；无跟踪测试。 |
| `cyrene.tools.mcp` | `mcp_provider:McpToolProvider` | 模块不存在；无跟踪测试。 |

根因之一是 [`validator.py`](../../Cyrene-Plugins-Official/conformance/harness/validator.py) 只对 `GENERIC` 记录执行入口解析；`COMPATIBILITY_SHIM` 只检查声明字段、能力和 removal gate，所以这些入口仍可通过 Conformance。

### P0-2：Catalog Set 的 `RESOLVED` 与可运行性冲突

[`agent-tools.set.json`](../../Cyrene-Plugins-Official/catalog/sets/agent-tools.set.json) 把 `skill.runtime.v1`、`tool.provider.v1`、`message.connector.v1` 标为 `RESOLVED`，对应实现正是上表中不可加载的 Skills、MCP、IM。

[`gateway.set.json`](../../Cyrene-Plugins-Official/catalog/sets/gateway.set.json) 又把下面三项标为 `RESOLVED`：

- `gateway.runtime.v1`：候选是缺少 `main:app` 的 Python Gateway 与只返回 `503` 的 ASP.NET 骨架；
- `model.routing.v1`：候选入口不存在；
- `policy.evaluator.v1`：候选入口不存在。

在入口可加载、方法可调用和对应 TCK 通过之前，这些状态应降为 `DECLARED`、`INCUBATING` 或明确的 compatibility-only 状态。

### P0-3：自定义脚本插件越过 Kernel 进程监管边界

[`repository-policy.yaml`](../../Cyrene-Plugins-Official/repository-policy.yaml) 明确声明 Plugins 不拥有 Generic Kernel process supervision；但 [`custom_script_runner.py`](../../Cyrene-Plugins-Official/plugins/training/custom-script-runner/custom_script_runner.py) 自己维护 job/process/thread 状态，直接 `subprocess.Popen`、轮询、kill/terminate，并从父进程复制全部环境后叠加调用方传入的环境。

这段实现没有容器、cgroup、权限、文件系统、网络或环境隔离，Workspace 旧报告却称其为“沙箱启动与销毁”。它应改成通过 Platform/CES 的受管执行契约发起任务；在完成前只能标记为本地开发 runner。

### P0-4：模拟路径会把未运行的真实能力写成成功

| 位置 | 疑点 | 影响 |
| --- | --- | --- |
| `plugins/engines/tensorrt-llm/tensorrt_llm_engine.py:46-70,100-111` | 缺少 `tensorrt_llm` 时自动进入 mock；不存在的模型目录仍返回 `LOADED_MOCK`；唯一测试接受 `LOADED` 或 `LOADED_MOCK`。 | CPU CI 绿不能证明 TensorRT、GPU、KV Cache、量化或真实推理。 |
| `plugins/tools/seedance-video-gen/seedance_video_gen.py:79-117`、`_adapter.py:143-167` | `mock=True` 是默认值；无真实配置时返回公共样例视频 URL。 | 调用方若未显式检查 `mock`，会把样例结果当成生成结果。 |
| `plugins/environment/docker-uv-builder/docker_uv_builder.py:137-153,202-215` | Docker 不存在时自动返回模拟 build/publish 计划。 | 已返回 `is_simulated`，但上层若只看成功结果仍会误判镜像已构建。 |

生产入口应显式选择 mock，并在真实能力缺失时 fail closed；CI 应把模拟测试与真实硬件/服务验收分开记账。

## 4. 22 个插件逐项盘点

| Plugin | 边界 / 证据状态 | 疑似老代码或不完整点 |
| --- | --- | --- |
| `cyrene.agents.agent-system` | Shim / `INCUBATING` | 入口不存在；测试收集失败；大量 AstrBot API、TODO、弃用方法和进程/工具循环代码。 |
| `cyrene.agents.skills-runtime` | Shim / `DECLARED` | 独立 wheel 缺少运行所需 `astrbot.core.utils`；测试收集失败。 |
| `cyrene.connectors.im` | Shim / `DECLARED` | 入口不存在、无测试；仍是 AstrBot Python/.NET 平台适配快照。 |
| `cyrene.connectors.onebot-v11` | Generic / `CI_VERIFIED` | 核心测试可运行；主要噪声为生成的 protobuf 文件，不视为手工老代码。 |
| `cyrene.engines.tensorrt-llm` | Generic / `DECLARED` | 缺库自动 mock；无 TensorRT 依赖声明；测试只证明 mock 生命周期。 |
| `cyrene.environment.docker-uv-builder` | Generic / `DECLARED` | 缺 Docker 自动模拟；真实 daemon/image/push 未纳入主 CI。 |
| `cyrene.gateway.aspnetcore` | Generic skeleton / `DECLARED` | 只暴露 `/health/live` 与 catch-all `503`；manifest 宣称 `start_server`、`forward_traffic`；`.NET >=8` 声明与 `net10.0` 项目不一致。 |
| `cyrene.gateway.prompt-cache` | Generic / `DECLARED` | 名称含 “Semantic Cache”，实现仅对 prompt/model/参数做 SHA-256 精确缓存；无语义相似度路径、无测试、无独立构建元数据。 |
| `cyrene.gateway.python` | Shim / `DECLARED` | `main:app` 不存在；仍是 legacy filter 与兼容 worker。 |
| `cyrene.gateway.spring` | Product snapshot / `DECLARED` | 已登记 relocation debt；仍含租户、计费、训练、部署与 worker 语义；4 处业务 TODO、1 个空断言测试、构建约束冲突。 |
| `cyrene.models.hf-analyzer` | Generic / `CI_VERIFIED` | 未发现实质性遗留入口；宽异常捕获属于结果归一化边界，可后续收窄。 |
| `cyrene.policy.circuit-breaker` | Generic / `DECLARED` | 实现是内存三态机，无健康探测或真实 failover；无测试、无独立构建元数据。 |
| `cyrene.policy.compat-rules` | Generic / `CI_VERIFIED` | 本轮未发现实质性老代码。 |
| `cyrene.policy.content-safety` | Shim / `DECLARED` | 入口不存在、无测试；保留 AstrBot moderation/rate-limit 快照。 |
| `cyrene.policy.model-routing` | Shim / `DECLARED` | 入口不存在、无测试；保留 ProviderManager/.NET router 快照。 |
| `cyrene.providers.model-api-connector` | Generic + compat / `CI_VERIFIED` | Generic core 可测试；仍有 29 个兼容源码文件。`embeddings.py` 的 3 个 `NotImplementedError` 是子类 hook，不列为清理缺陷。 |
| `cyrene.tools.dataset-validator` | Generic / `DECLARED` | Manifest 宣称 JSONL、JSON、CSV；实现只支持 JSONL/JSON，CSV 会进入 `Unsupported format`；无测试和构建元数据。 |
| `cyrene.tools.mcp` | Shim / `DECLARED` | 入口不存在、无测试；只有 3 个 AstrBot MCP 兼容源码文件。 |
| `cyrene.tools.media` | Generic + compat / `CI_VERIFIED` | Generic core 与 Platform TCK 可运行；仍有 9 个兼容源码文件和重复的 Shiki/template 资产。 |
| `cyrene.tools.seedance-video-gen` | Generic + sample SDK / `DECLARED` | 默认 mock、无测试；构建出的 wheel 没有任何代码 payload；仍支持一组未建模的 legacy task aliases。 |
| `cyrene.training.custom-script` | Generic / `DECLARED` | 直接拥有进程生命周期且无隔离；异常被静默吞掉；只有 happy-path 子进程测试。 |
| `cyrene.training.distributed-deepspeed` | Generic / `DECLARED` | 仅生成配置、显存启发式和 `torchrun` 命令；不调用 DeepSpeed、torchrun 或 GPU，依赖列表为空；“orchestrator”表述过强。 |

## 5. 已登记兼容与迁移代码全量目录

这些目录是明确记录的技术债，不能当作“暗藏未登记老代码”，但也不能从“archive 已删除”推导为迁移完成。统计只计算 `.py/.cs/.kt/.java/.js/.ts/.rs/.go` 源码。

| Plugin | Boundary Registry 中的兼容路径 | 源码文件 | 行数 |
| --- | --- | ---: | ---: |
| `cyrene.agents.agent-system` | `src/astrbot`; `src/dotnet`; `compatibility/python` | 177 | 42,932 |
| `cyrene.agents.skills-runtime` | `src/astrbot` | 3 | 1,301 |
| `cyrene.connectors.im` | `src/python/astrbot`; `src/dotnet` | 42 | 10,288 |
| `cyrene.gateway.aspnetcore` | `compatibility/AstrBot.DotNetHost` | 227 | 80,843 |
| `cyrene.gateway.python` | `python_gateway_lite.py`; `compatibility` | 41 | 18,459 |
| `cyrene.policy.content-safety` | `compatibility` | 23 | 2,706 |
| `cyrene.policy.model-routing` | `compatibility` | 4 | 2,674 |
| `cyrene.providers.model-api-connector` | `compatibility/python/astrbot` | 29 | 5,599 |
| `cyrene.tools.mcp` | `compatibility` | 3 | 1,138 |
| `cyrene.tools.media` | `compatibility` | 9 | 3,458 |
| `cyrene.tools.seedance-video-gen` | `javaSDK/doubao-seedance-2.0(java).txt`; `javaSDK/javaSDK/MaasSeedanceDemoTest.java` | 1 | 154 |
| **合计** | **11 组路径** | **559** | **169,552** |

额外有 4 份 migration marker 仍处于 `pending` 或 `compatibility-snapshot`：

- `plugins/gateway/aspnet-core/docs/CYRENE_MIGRATION_MARKER.md`
- `plugins/gateway/spring/docs/CYRENE_MIGRATION_MARKER.md`
- `plugins/policy/model-routing/docs/CYRENE_MIGRATION_MARKER.md`
- `plugins/providers/model-api-connector/docs/CYRENE_MIGRATION_MARKER.md`

Agent System、Skills 和 IM 的兼容代码位于 `src/...`，不在名为 `compatibility` 的目录内；它们靠 Boundary Registry 才能被识别为 shim。

## 6. 源码级疑点清单

### 6.1 未实现、TODO、占位和弃用

| 文件 | 位置 | 判断 |
| --- | --- | --- |
| `plugins/agents/agent-system/src/astrbot/core/agent/tool.py` | `83,173,199,204,343,347,351` | 抽象未实现与多组已弃用 API，属于 shim 清退债务。 |
| `plugins/agents/agent-system/src/astrbot/core/computer/booters/local.py` | `444,449` | 未实现平台相关操作。 |
| `plugins/agents/agent-system/src/astrbot/core/message/components.py` | `71-76,441,448,455,473,482,609-627` | 5 组 TODO 消息组件与 legacy/deprecated 字段。 |
| `plugins/agents/agent-system/src/astrbot/core/pipeline/content_safety_check/strategies/__init__.py` | `19` | 抽象策略 hook；随 shim 管理。 |
| `plugins/agents/agent-system/src/astrbot/core/pipeline/preprocess_stage/stage.py` | `190` | 未闭环的职责拆分 TODO。 |
| `plugins/agents/agent-system/src/astrbot/core/pipeline/stage.py` | `41,56` | 抽象 stage hook；随 shim 管理。 |
| `plugins/agents/agent-system/src/dotnet/AstrBot.DotNetHost/Services/Ai/IAiClient.cs` | `57,66,75` | rerank/TTS/STT 默认返回 `NotSupportedException`。 |
| `plugins/connectors/im/src/python/astrbot/core/platform/platform.py` | `136,144,212-214` | 多个未实现平台方法。 |
| `plugins/connectors/im/src/dotnet/Services/Platforms/Wecom/WecomAiBotWebhookService.cs` | `138,190` | 图片处理仍是 placeholder。 |
| `plugins/gateway/spring/src/main/kotlin/com/cy/llm/gateway/alert/AlertService.kt` | `454` | 读取告警时把 severity 固定为 `WARNING`。 |
| `plugins/gateway/spring/src/main/kotlin/com/cy/llm/gateway/billing/BillingController.kt` | `136` | 状态接口把 pricing count 固定为 `0`。 |
| `plugins/gateway/spring/src/main/kotlin/com/cy/llm/gateway/billing/BillingService.kt` | `481` | `usageByDay` 固定为空列表。 |
| `plugins/gateway/spring/src/main/kotlin/com/cy/llm/gateway/tenant/QuotaService.kt` | `153` | `actualTokens` 尚未修正预估扣费。 |
| `plugins/gateway/spring/src/test/kotlin/com/cy/llm/service/InferenceServiceTest.kt` | `24-27` | 测试只有 `assertTrue(true)`。 |
| `plugins/training/custom-script-runner/custom_script_runner.py` | `176-205,240-242` | 宽异常捕获；日志 JSON 解析和 pipe reader 异常直接 `pass`，可观测性不足。 |

`plugins/providers/model-api-connector/src/model_api_connector/embeddings.py:364-375`、Spring `DatabaseConfig.kt:34` 以及基类中的部分 `NotImplementedError`/`UnsupportedOperationException` 是显式扩展点或框架保护，不建议仅凭关键字删除。

### 6.2 重复和大体积兼容资产

Agent System 与 Media 的兼容树各自跟踪了完全相同的 4 份 T2I 资产：

- `shiki_runtime.iife.js`：每份 1,267,458 bytes；
- `astrbot_vitepress.html`：每份 14,132 bytes；
- `base.html`：每份 7,407 bytes；
- `astrbot_powershell.html`：每份 5,122 bytes。

这 4 组重复文件合计可避免约 1.29 MB 的重复跟踪。若仍需兼容，应指定单一来源或生成规则；不要继续分别修改两份快照。

### 6.3 Python 质量债务

对 292 个跟踪 Python 文件执行 Ruff 0.16.5：

- `ruff check --statistics`：765 项；其中 `BLE001` 215、`TRY002` 74、旧式 typing 133（`UP006/UP035/UP045`）、未排序 import 61、异步函数阻塞文件 I/O 22、未使用 import 21、静默 `except` 27（`S110/S112`）、可变类默认值 8、未显式 `check` 的 subprocess 5；
- 265 项可安全自动修复，另有 57 项仅能 unsafe-fix；
- `ruff format --check`：24 个文件需要格式化；
- 仓库未跟踪统一 Ruff 配置，主 CI 也没有 Ruff/format job。

大量告警来自已登记的 AstrBot 兼容快照和 protobuf 生成文件。建议先对 `GENERIC implementationPaths` 建强制 gate，再为生成和兼容路径配置有理由、有截止条件的排除项。

## 7. 构建与打包疑点

1. **Seedance wheel 为空**：`uv build --wheel` 成功退出，但 wheel 的非 metadata payload 为 0。原因是 `pyproject.toml` 使用 package discovery，而实现是顶层 `seedance_video_gen.py`、`_adapter.py`、`_upload.py`，没有声明 `py-modules`。
2. **Manifest 未进入 Python wheel**：对 9 个 manifest-root Python 项目构建后，没有任何 wheel 包含 `plugin.manifest.json`。若 Catalog/package installer 不在安装时注入 manifest，独立分发包就失去唯一 metadata authority。
3. **11/22 个插件根没有独立构建元数据**：包括 IM、Docker Builder、Prompt Cache、Python Gateway、HF Analyzer、Circuit Breaker、Compat Rules、Content Policy、Model Routing、Dataset Validator、MCP。需要明确它们是源码型插件还是缺少 package contract。
4. **ASP.NET 版本矛盾**：manifest/catalog 写 `.NET >=8.0`，项目实际 target `net10.0`；实现只含 health 与统一 `503`，并未实现 manifest 描述的 YARP 转发。
5. **Spring 版本与 Wrapper 矛盾**：manifest 写 Java `>=17`，README 写 JDK 21，Gradle 强制 Java/JVM 25；`gradlew` 是 `100644` 且缺 `gradle-wrapper.jar`，`./gradlew` 和 `bash gradlew` 都不能完成构建。
6. **Release 声明无实现**：Repository Policy 写 `automated_release: true` 和 GitHub Releases authority，但仓库只有 `ci.yml`、`cross-repo.yml`，没有 release/package publish workflow，当前也没有 tag。

独立验证中，ASP.NET 主项目和 Agent compat C# 项目都以 Release + warnings-as-errors 构建通过；Spring 使用已缓存的系统 Gradle 9.5 完成 109 tests、0 failures、0 errors、2 skipped，但编译器仍报告 deprecated API、unchecked cast、恒真/恒假条件以及 Gradle 10 不兼容提示。这些通过结果不能修复上面的分发与能力边界问题。

## 8. Catalog、Lifecycle 与治理文档漂移

### 8.1 数量和时间戳不一致

- Canonical manifests、Catalog、Boundary Registry 都是 22 条；`plugins-lifecycle.yaml` 只有 15 条，缺少以下 7 个 ID：
  - `cyrene.engines.tensorrt-llm`
  - `cyrene.gateway.prompt-cache`
  - `cyrene.policy.circuit-breaker`
  - `cyrene.tools.dataset-validator`
  - `cyrene.tools.seedance-video-gen`
  - `cyrene.training.custom-script`
  - `cyrene.training.distributed-deepspeed`
- Lifecycle 的 `updated_at` 仍是 2026-08-27；Catalog 的 `updatedAt` 是 2026-08-30，而部分条目在 2026-09-07 才加入。
- `docs/governance/plugin-boundary-inventory.md` 仍写“14 个 manifest”，并记录 4 个本地 `plugins/data/*` manifest；当前 checkout 中这些目录和文件都不存在。该文档的 active inventory 也只列 14 个插件。

### 8.2 Repository Policy 与远端事实不一致

| 字段 | Policy | 当前远端事实 |
| --- | --- | --- |
| visibility | `public` | `PRIVATE` |
| default branch | `develop` | GitHub default 为 `main`；集成分支仍是 `develop` |
| CI authority | `github` | 当前 GitHub run 因账户付款/额度问题 4 个 job 均 0 steps；同 SHA 的 Azure build 425 成功 |
| build systems | `pyproject`, `gradle` | 仓库还含 2 个实际参与验证的 `.csproj` |
| automated release | `true` | 无 release workflow、无 tag |

GitHub run 34167878395 的失败属于账户/CI 环境阻塞，不能算源码失败；Azure build 425 只覆盖有限矩阵，也不能升级为全仓 PASS。

### 8.3 Evidence Validator 结论过强

`conformance/harness/validate_evidence.py` 对 `CI_VERIFIED` 只检查：证据字符串非空，且包含 `::` 或单词 `test`。它不确认文件/测试节点存在，也不读取执行结果，却输出 `All catalog evidence declarations are strictly verified`。当前 22 个 Catalog 条目中只有 5 个 `CI_VERIFIED`、16 个 `DECLARED`、1 个 `INCUBATING`；应把 validator 文案和逻辑改成“metadata reference validation”，或接入不可变 CI ledger。

### 8.4 Backlog 和贡献文档中的过期路径

下面这些文件仍引用不存在的 `plugins/data/memory`、旧名 `spring-gateway` 或“等待 Platform Plugin Contract 定稿”：

- `docs/contributing/issue-backlog.md`
- `docs/contributing/issues/002-memory-sqlite-schema-migration.md`
- `docs/contributing/issues/003-spring-gateway-oci-packaging.md`
- `docs/contributing/issues/008-inline-plugin-canonical-contract-adoption.md`
- `docs/contributing/issues/009-worker-plugin-handshake-conformance.md`
- `docs/contributing/issues/010-tck-ipc-cancellation-test-suite.md`
- `docs/contributing/issues/011-production-vllm-serving-engine-adapter.md`
- `docs/contributing/issues/012-production-training-engine-adapter.md`
- `docs/contributing/plugin-work-items.md`
- `docs/contributing/first-plugin-contribution.md`
- `docs/governance/astrbot-extraction-readiness.md`
- `docs/governance/plugin-contract-readiness.md`
- `docs/governance/third-party-dependency-inventory.md`

这些 backlog 不一定都已完成；需要逐项改成“仍有效”“已被替代”“路径已移除”或“已完成”，否则新贡献者会向不存在的目录提交代码。

## 9. 测试与 CI 覆盖账本

| 验证 | 当前结果 | 能证明什么 |
| --- | --- | --- |
| 默认 `uv run --frozen python -m pytest -q` | 43 passed, 7 skipped | 只证明默认 `conformance/tests` 子集；skip 都与 Platform resolver/worker gate 有关。 |
| 指向当前 Platform `develop@a402b7b8` 的 Conformance | 50 passed | 当前 manifest/schema/purity 与跨仓 resolver TCK 通过。 |
| TensorRT + Docker Builder + Custom Script + DeepSpeed | 9 passed | 只证明 mock、计划生成和本地 happy path；没有真实 GPU/Docker/DeepSpeed。 |
| Model Provider | 109 passed | 当前 provider 单测通过；live API 仍需凭据。 |
| Connector package lifecycle | 4 passed | package/binding 生命周期子集通过。 |
| OneBot | 21 passed | connector/transport 测试通过。 |
| Python Gateway compatibility worker | 85 passed | 兼容 worker 子树通过；不证明 manifest 的 `main:app`。 |
| Agent System | collection error | 缺 `astrbot.core.provider`。 |
| Skills Runtime | 3 collection errors | 缺 `astrbot.core.utils`。 |
| Spring | 109 passed, 2 skipped | 需要系统/缓存 Gradle；完整 Spring context 集成测试被禁用。 |
| 两个 .NET 项目 | 0 warnings, 0 errors | 只覆盖两个 `.csproj` 明确包含的源集；其余大量 C# 快照未编译。 |

README 和 CONTRIBUTING 都建议直接运行 `python -m pytest`，但根 `pyproject.toml` 把 `testpaths` 限定为 `conformance/tests`。主 GitHub/Azure CI 也只覆盖 Conformance、两个 evaluator、Provider、Connector lifecycle 和 OneBot；未覆盖 Ruff、format、Spring、.NET、Agent、Skills、Gateway worker、TensorRT、Docker Builder、Prompt Cache、Circuit Breaker、Dataset Validator、Seedance、Custom Script、DeepSpeed。

`.github/workflows/cross-repo.yml` 固定测试 Platform `2924c64b`；当前 Platform `develop` 是其后继 `a402b7b8`。固定 accepted SHA 可以保留用于可重现基线，但 scheduled/current-compatibility gate 还应另测最新受支持分支。

## 10. Workspace 内需要同步纠正的旧结论

本报告取代 [`cyrene-architecture-refactor-report.md`](plans/cyrene-architecture-refactor-report.md) 中关于 Plugins 当前交付状态的表述。该文件需要后续修订的地方包括：

- 第 27 行称 3 个插件“100% Conformance 与单元测试验证”；TensorRT 测试只走 mock，DeepSpeed 未执行真实训练，Custom Script 没有沙箱。
- 第 35 行宣称 Inflight Batching、KV Cache、FP4/FP8/INT4 已交付；当前代码只是把参数传给 `ModelRunner.generate`，没有相应真实验收。
- 第 47 行称 Custom Script 在沙箱中运行；当前是继承父环境的普通 `Popen`。
- 第 109-115 行把三个 Gateway 写成面向生产/高性能实现；ASP.NET 是骨架、Python 入口不存在、Spring 是明确排除安装推荐的 Product 快照。
- 第 113 行称 Prompt Cache 支持语义缓存；当前只有精确哈希缓存。
- 第 137-139 行称 manifest 严密合规并写“40 项 + 5 项全 PASS”；当前更准确的账本是本报告第 9 节，且 manifest schema 通过不等于入口可加载。

Workspace 自己还有两处术语漂移：`docs/README.md` 仍把插件规范写成 `plugin.toml` V1，`docs/COMPATIBILITY.md` 也写迁移到 `plugin.toml` V1；当前 canonical authority 已是 `plugin.manifest.json`。

## 11. 建议处置顺序

1. **先修真值**：把不可加载 shim 与 Catalog Set 的 `RESOLVED` 状态对齐；Lifecycle 补齐 22 条；Evidence Validator 改成诚实表述。
2. **关闭误成功路径**：真实模式缺依赖时 fail closed；mock 只允许显式开发配置，并在返回结构和证据 ledger 中强制标明。
3. **恢复边界**：Custom Script 通过 Platform/CES 执行；Spring Product snapshot 制定迁出 owner、目标仓和 removal gate。
4. **修入口与独立打包**：给 7 个 shim 决定“补 adapter 入口”或“取消可安装声明”；修复空 Seedance wheel，并明确 manifest 如何随包分发。
5. **建立分层 CI**：Generic 快速 gate、Compatibility gate、跨仓最新/固定基线 gate、Spring/.NET build、真实 GPU/服务验收分别记账，禁止把 skipped/mock 当 PASS。
6. **最后清样式与重复资产**：先处理 Generic 的 Ruff/format，再收缩兼容快照、合并重复 T2I 资产，避免大规模格式化掩盖语义修改。

## 12. 明确未发现的遗留物

- 无跟踪中的根级 `archive/` 或 `legacy/` 目录；
- 无 `plugin.legacy.toml`、`*.legacy.toml`、`LEGACY_PLUGIN.md`；
- 无跟踪的 `.pyc`、`.class`、`.jar`、build/cache/log/temp/bak 文件；
- 无跟踪空文件；
- 除 4 组已列出的 T2I 资产外，未发现其他完全相同的源码 blob；
- 当前 Plugins 与 Workspace 工作树在审计前均干净。

这些阴性结果只覆盖 `develop@0b13720e` 的跟踪内容，不覆盖被 `.gitignore` 排除的本地文件、远端历史分支或未合并 PR。

## 13. 扩展到全仓后的结论

当前项目可以进入“闭环后的清理阶段”，但还不适合直接把现状冻结成下一阶段的干净起点。全仓最主要的问题已经不是根目录里是否还有名为 `legacy/` 的文件夹，而是下面六类仍可达或仍被治理文件当作当前事实的旧实现：

1. **会返回成功的未实现路径**：DH-System-Internal 的模型基类和云连接器仍可能把“配置存在”或“占位返回”写成健康/成功；Plugins 的 TensorRT、Seedance、Docker Builder 也有相同类型问题。
2. **职责已经迁移，但旧 owner 仍提供活动服务**：Exchange 的 gRPC server 仍注册训练与自定义脚本服务；Reactor 仍注册 Hybrid 和具体 TensorRT 引擎，尽管架构文档已把具体引擎列为插件抽取目标。
3. **明确可运行的兼容产品仍留在仓库**：Astrbot-Rev 仍可构建 `python-compat` 镜像，包含约 5.1 万行根 Python runtime；它是有开关的迁移面，不是不可达垃圾。
4. **产品壳仍以 mock 数据为唯一数据源**：Navigator Windows 原生可执行原型直接实例化 mock 会话和 agent 状态；它可以作为设计原型保留，但不能作为真实 Product 闭环证据。
5. **源码已经删除，权威文档仍指向旧路径**：Exchange、Catalyst、Echo、Navigator 以及 Workspace 数据库登记仍引用已经不存在的 `legacy/`、`legacy-dh/` 路径。
6. **仓库治理元数据没有随第一次闭环收敛**：远端 visibility/default branch、Repository Policy、Workspace 拓扑、build systems、accepted baseline 和真实 CI authority 之间仍有系统性偏差。

本扩展审计没有把以下内容直接判成老代码：抽象接口的显式 hook、测试 double、生成的 protobuf/grpc 源码、Python namespace 空 `__init__.py`、固定可重现依赖 SHA，以及有清晰来源和同步策略的 vendored 代码。它们只有在与实际可达性、声明或 owner 冲突时才进入问题台账。

## 14. 全仓审计快照

本轮以 [`repositories.yaml`](../repositories.yaml) 的 integration branch 为规范入口，并实时回读远端分支。Astrbot-Rev 与 DH-System-Internal 的常驻本地 checkout 不在规范分支，因此分别使用干净的精确 checkout 审计 `develop` 和 `main`；没有在这些仓库写入文件。

| Repository | 审计基线 | 跟踪文件 | 源码文件 / 行数 | 当前 Hosted CI 证据 |
| --- | --- | ---: | ---: | --- |
| Workspace | `main@2e53b7fc` | 135 | 16 / 4,216 | Azure 431，成功 |
| Platform | `develop@a402b7b8` | 866 | 298 / 99,763 | 合并前精确源码树 `cd454002` 的 Azure 401 成功；合并 SHA 无 exact-head Azure run |
| Plugins | `develop@0b13720e` | 1,029 | 721 / 207,180 | Azure 425，成功 |
| Astrbot-Rev | `develop@8787db7a` | 1,760 | 793 / 243,948 | GitHub Actions 33997906381，成功 |
| DH-System-Internal | 拓扑指定 `main@e10f998f` | 178 | 83 / 19,124 | 无对应 Azure Pipeline definition/run |
| Reactor | `develop@3b8d4e92` | 277 | 142 / 30,372 | Azure 427，成功 |
| Yield | `develop@c71057f6` | 709 | 392 / 76,569 | Azure 388，成功 |
| Exchange | `develop@3379bb4e` | 137 | 60 / 13,433 | Azure 428，成功 |
| Catalyst | `develop@cf568ae9` | 43 | 14 / 3,505 | Azure 432，成功 |
| Echo | `develop@8f51170e` | 41 | 13 / 3,993 | Azure 426，成功 |
| Navigator | `develop@41a42e86` | 198 | 80 / 24,982 | Azure 433，成功 |
| **合计** | **11 个仓库** | **5,373** | **2,612 / 727,085** | 见第 25 节的口径限制 |

DH-System-Internal 同时存在更靠前的远端默认分支 `develop@ad52b2d7`。由于 Workspace 仍把 `main` 写成 integration branch，本报告把 `main` 作为规范基线，同时额外复核 `develop`，以免把已在开发分支修掉的问题误写成当前开发事实。`Reference-code/` 不在 canonical topology 中，因此没有纳入本轮结论。

## 15. 跨仓清理优先级台账

| ID | 优先级 | Repository / 路径 | 当前判断 | 建议处置 |
| --- | --- | --- | --- | --- |
| X-01 | P0 | DH `main` 与 `develop` | Workspace 指定 `main`，远端 HEAD 与仓库 Policy 指定 `develop`；两者相差 110 个文件、8,577 行新增和 389 行删除。当前没有单一“现在版本”。 | 先决定 integration authority，再更新 Workspace、Policy、CI 与 accepted baseline；清理工作只在选定分支进行。 |
| X-02 | P0 | DH `OpenAiCompatibleModelProvider`、Azure provider | `CheckHealthAsync` 可直接返回健康；基础 Chat 返回“not implemented”正常终态，Embed 返回空向量；Azure 配好 ID 后可报健康，但四个 capability service 都是 `null`。 | 所有未实现能力 fail closed；health 必须证明一次真实能力探测，不能只证明配置字段存在。 |
| X-03 | P0 | Exchange `components/coordinator` | 活动 gRPC server 注册 `CoordinatorTrainingService`，实现训练与自定义脚本排队、worker 选择和取消；Repository Policy 明确只拥有 gateway/routing。 | 从 Exchange 移出训练/custom-script authority；保留的 gateway 只消费 Yield/Platform 契约。 |
| X-04 | P0 | Reactor `hybrid_engine.py` | `hybrid` 仍在 Core 与 Pro selector 注册；它捕获所有加载异常后换硬件 backend，可能掩盖模型或配置错误。 | 删除活动注册或改成显式、有类型的 placement policy；禁止用宽异常实现跨硬件静默 fallback。 |
| X-05 | P0 | Plugins 第 3 节问题 | 7 个不可独立加载 shim 被 Catalog 写成 `RESOLVED`，且 Custom Script 越过 Kernel 边界。 | 按第 11 节顺序先修状态真值和进程边界。 |
| X-06 | P1 | Reactor TensorRT / Plugins TensorRT | Reactor 内含 400+ 行具体 TensorRT-LLM engine 并主动注册；架构文档又把它列为可替换 plugin，Plugins 同时提供另一实现。 | 明确唯一实现 owner：Reactor 保留 Product lifecycle/selection，具体 engine 通过一个插件契约提供。 |
| X-07 | P1 | Reactor Pro 与 sidecar | LoRA load/unload/activate 只是 `sleep` 和状态记账；UDS token count 是占位；sidecar telemetry 返回 0、未发送 coordinator、未应用反馈。 | 从 production profile 移除或强制标记 simulated；补真实 backend 后再提升状态。 |
| X-08 | P1 | Astrbot-Rev `python-compat` | 239 个根 Python runtime 文件、约 50,701 行，仍能构建独立兼容镜像；兼容删除门槛未完成。 | 保留成明确 profile 并建立调用/用户计量、截止版本和删除 gate；不要继续与 .NET 主路径双向演进。 |
| X-09 | P1 | Navigator Windows `Core/Mock` | 5 个 mock 文件、1,568 行；`MainWindow` 直接 `new MockConversationService()` / `new MockAgentState()`。 | 将 prototype 与 Product build/release profile 隔离；真实 API adapter 完成前状态保持 prototype。 |
| X-10 | P1 | Workspace `governance/databases.yaml` | 4 处 container/development authority 指向已删除的 Navigator `legacy-dh/apps/control-plane/docker-compose.yml`。 | 指向实际 owner 的 compose/deployment manifest，或删除不再成立的本地容器权威。 |
| X-11 | P1 | Exchange/Catalyst/Echo/Navigator 文档 | 四个仓库都声称已删除的 legacy 目录仍存在；API 状态表仍把它们列成 `COMPATIBILITY_SHIM`。 | 同代码删除一起修 README/API/architecture；历史事实移入带日期、非权威 ledger。 |
| X-12 | P1 | 全部 GitHub 成员仓库 Policy | 9 个 Policy 都写 `visibility: public`、`remote_default_branch: develop`，实际全部为 PRIVATE、默认分支全部为 `main`。 | 选择修远端还是修 Policy；字段必须能被自动回读验证。 |
| X-13 | P1 | Workspace `accepted-baseline.yaml` | 11 个条目中 10 个不再等于当前规范分支，仅 DH `main` 相同。 | 若它代表 current accepted baseline，就在本次闭环后更新；若是历史快照，则版本化文件名并增加 current pointer。 |
| X-14 | P1 | Platform compatibility infrastructure | 16 个文件、2,225 行；两个 K8s renderer 仅差一个空行，其中一份又与 Astrbot 完全相同。 | 建一个生成器 authority，其余仓库只消费生成产物或固定版本。 |
| X-15 | P1 | Platform JVM integration | JVM 全链路测试永久 `#[ignore]`，依赖缺失时提前返回，最终只打印 skeleton present。 | 独立成环境 gate 并在 CI ledger 记 `NOT_RUN`；不要让 test discovery 代替执行。 |
| X-16 | P2 | Yield vendored LLaMA Factory | 586 个跟踪文件、约 64,211 行 Python；仍带上游 API/eval/chat 等非训练源码、空模块/文档，且 Policy 写明不拥有具体 engine internals。 | 建立 upstream commit、patch queue、保留模块和裁剪清单；决定独立 fork 还是 Official plugin。 |
| X-17 | P2 | 治理/build metadata | Catalyst/Echo 的 Policy 与 Workspace 都写空 build systems；Navigator 的 Workspace 条目也为空；Astrbot Policy 漏 .NET；Exchange Policy 多写不存在的 Cargo。 | 从实际构建入口生成/验证元数据，避免下一阶段 CI 漏项。 |

## 16. 系统性治理漂移

### 16.1 远端 visibility 与默认分支

实时 `gh repo view` 显示 Platform、Plugins、Astrbot-Rev、Reactor、Yield、Exchange、Catalyst、Echo、Navigator 全部为 `PRIVATE`，远端默认分支全部是 `main`。这 9 个仓库的 `repository-policy.yaml` 却都声明 `visibility: public` 与 `remote_default_branch: develop`。

Workspace [`repositories.yaml`](../repositories.yaml) 只把 Plugins 标为 private，其余 8 个 GitHub 成员标为 public；因此当前自动 clone policy、公开依赖判断与新 PR 默认 base 都可能建立在错误事实上。`integration_branch: develop` 可以继续保留，但它不能被写成当前 remote default。

DH 的方向相反：远端 HEAD 和仓库 [`repository-policy.yaml`](../../Services/DH-System-Internal/repository-policy.yaml) 都是 `develop`，Workspace 却仍指定 `main`。这会让全仓脚本、IDE checkout、CI 证明和清理 PR 分别落到不同代码树。

### 16.2 build systems 与交付物登记

- Catalyst 和 Echo 都有可构建 wheel 的 `pyproject.toml`，但各自 Policy 以及 Workspace 都写 `build_systems: []`、`package_units: []`。
- Navigator 的仓库 Policy 只写 Cargo/NPM，遗漏根 Python package 和 Windows `.csproj`；Workspace 则整项写空。
- Astrbot-Rev 的 production composition root 是 .NET，Policy 的 build systems 却只有 pyproject/NPM。
- Exchange Policy 写 Gradle/pyproject/Cargo，但仓库没有 `Cargo.toml`；Workspace 的 Gradle/uv 更接近当前事实。
- 所有这些 Policy 都把 CI authority 写成单一 GitHub 或 Azure，但实际第一次闭环主要由 Azure Hosted 承担，GitHub 同 SHA 因账户问题未启动。

### 16.3 accepted baseline 的语义不明确

[`governance/accepted-baseline.yaml`](../governance/accepted-baseline.yaml) 的时间戳仍是 2026-08-30。实时比较显示 Workspace、Platform、Plugins、Astrbot-Rev、Yield、Reactor、Exchange、Catalyst、Navigator、Echo 的记录 SHA 都已落后，只有 DH `main` 相同。

静态 accepted baseline 可以是合法、不可变的历史证据，但当前文件名和 description 都没有说明它是历史快照。第一次完整闭环后，应把“历史可复现 baseline”和“当前被接受 baseline”分开，否则后续 guard 可能继续验证旧树。

## 17. Cyrene-Platform 详查

### 17.1 兼容基础设施仍有多个副本

[`infrastructure/**/compatibility`](../../Cyrene-Platform/infrastructure) 下仍跟踪 16 个文件、2,225 行，包括 Astrbot/NapCat Kubernetes manifests、NGINX 配置和 Docker runtime template。这些路径有明确 compatibility 名称，可以保留，但需要 owner、消费方、删除门槛与生成规则。

下面三份 renderer 形成重复权威：

- `infrastructure/kubernetes/compatibility/render_k8s_manifests.py`：677 行；
- `infrastructure/kubernetes/compatibility/scripts/render_k8s_manifests.py`：676 行，只比上一份少末尾空行；
- Astrbot-Rev `scripts/render_k8s_manifests.py`：与 Platform 的 `scripts/` 版本 SHA-256 完全相同。

继续保留三份会使安全修复和 manifest 语义发生分叉。应只维护一份工具，并在消费仓固定版本或生成产物。

### 17.2 空类型和跳过测试

[`cy-local-transport/src/lib.rs:153`](../../Cyrene-Platform/framework/crates/cy-local-transport/src/lib.rs) 仍导出没有字段或实现的 `UnixSocketTransport` 与 `WindowsNamedPipeTransport`。如果它们不是公开兼容类型，应删除；若必须保 ABI，则补 deprecation/removal version，避免把类型存在误当作 IPC 已实现。

[`jvm_integration_test.rs:18`](../../Cyrene-Platform/framework/crates/cy-extension-registry/tests/jvm_integration_test.rs) 的 JVM full wire-protocol lifecycle 被 `#[ignore]`，即使手动运行也会在缺 Java/JAR 时成功返回，真正 sandboxd 接线仍是 TODO，最后只打印 skeleton。该项应在能力账本中保持 `NOT_RUN`。

### 17.3 历史报告与当前规范代码混放

`docs/cyrene检查报告v1-0820.md`、`docs/cyrene检查报告v2-0820.md`、`docs/adr/ADR-LEGACY-CY-LLM-CUTOVER.md` 仍位于普通 docs 导航空间。它们有调查价值，但应明确标记 superseded/dated，避免和当前架构规范竞争。

## 18. Astrbot-Rev 详查

### 18.1 根 Python runtime 是明确可运行的兼容产品

精确 `develop@8787db7a` 的 [`docs/MIGRATION_STATUS.md`](https://github.com/DoHorizon-AI/Astrbot-Rev/blob/8787db7aeeffcea9d86b5284d3c51aaca1b37149/docs/MIGRATION_STATUS.md) 已明确：.NET Host 是 canonical runtime，根 `main.py`/`astrbot/` 是 compatibility-only。当前仍跟踪 239 个根 runtime 文件、约 50,701 行 Python；[`Dockerfile:177-203`](https://github.com/DoHorizon-AI/Astrbot-Rev/blob/8787db7aeeffcea9d86b5284d3c51aaca1b37149/Dockerfile#L177-L203) 继续构建并启动 `python-compat` 镜像。

这棵代码不应按死代码直接删，因为它仍是显式 build target。下一阶段前应回答三个问题：谁仍使用该 target、哪些第三方插件尚未迁出、哪个版本之后允许移除。没有调用量与 removal gate 时，旧 Python 与新 .NET 会继续双向漂移。

### 18.2 兼容代码自身仍含未收口行为

根 Python runtime 仍有 plugin-extracted TODO、消息组件 TODO、`zip_updator`/cron/filter 的 `NotImplementedError` 和多处静默 `pass`。由于默认 production image 不复制该 runtime，这些问题优先级低于其移除决策；如果继续发布 `python-compat`，就必须把它当一个真实支持面执行独立测试，而不能只靠 .NET CI。

### 18.3 重复资产和旧迁移标记

- `scripts/render_k8s_manifests.py` 与 Platform 兼容目录中的副本完全相同；
- `astrbot/core/utils/t2i/template/shiki_runtime.iife.js` 与 Plugins 中两份副本完全相同，三个仓库副本合计约 3.80 MB；
- Dockerfile 顶部仍写本机 Windows 路径 `C:\Users\Baiji\DHDev\Cyrene\plugins\cy-plugin-docker` 和 `compatibility-retained` marker；
- `docs/en/use/astrbot-sandbox.md` 是 0-byte 文档，但 README 把 Agent Sandbox 作为正式功能链接出去；
- Repository Policy 漏掉实际 production `.NET` build system，并把仓库写成 public/develop-default。

## 19. DH-System-Internal 详查

### 19.1 先解决分支权威

Workspace 的规范基线 `main@e10f998f` 只有最初两次 .NET foundation 提交，没有 `repository-policy.yaml`、README、service manifest 或 pipeline YAML。远端默认 `develop@ad52b2d7` 已新增 Policy、产品文档、MCP/Azure DevOps 集成、真实 RunPod REST client 与更多测试，但仍未合并回 `main`。

在这个状态下，“按 main 清理”会重复修复 develop 已经改变的代码，“按 develop 清理”又不满足 Workspace 当前拓扑。X-01 是该仓所有清理工作的前置项。

### 19.2 develop 仍有会伪装成正常结果的基础实现

[`OpenAiCompatibleModelProvider.cs:19`](../../Services/DH-System-Internal/src/WeComAgentHub.CloudConnectors/ModelApi/OpenAiCompatibleModelProvider.cs) 的默认实现：

- 不做网络探测就返回 `HealthResult(true, "configured")`；
- Chat 以正常 final chunk 返回 `chat not implemented yet`；
- Embed 返回成功的空向量集合；
- ListModels 返回默认模型为可用。

目前只有 OpenRouter 子类继承它，并覆盖主要调用，但这些 public virtual 默认仍是危险 fallback。应改为 abstract 或抛出明确 `NotSupportedException`，避免以后新增 provider 忘记覆盖时产生假成功。

[`AzureHyperscaleProvider.cs:23`](../../Services/DH-System-Internal/src/WeComAgentHub.CloudConnectors/Hyperscale/AzureHyperscaleProvider.cs) 声明 Compute/Storage/Identity capability，四个 service property 却全部为 `null`；只要 Enabled/Tenant/Client/Subscription 非空就返回健康。它应标为 declared/not-ready，或在健康检查中验证一个真实 Azure 操作。

### 19.3 其余未实现面

- Auth、Application、AccountId、Doc、Drive、Media、Invoice、Interconnect、Security、Upstream 等 WeCom client 在未配置 tenant endpoint 时抛 `NotSupportedException`。这一行为本身是 fail closed，但 UI/API/Catalog 必须只公布实际配置可用的 action，不能把方法存在当成能力已接通。
- Plan 仍把 webhook/action stubs 写成“已完成”，同时真库 + 真企微 E2E 未运行；建议把“骨架完成”和“能力完成”拆开。
- develop Policy 写 `ci: azure_devops`，但当前 Azure project 没有 DH-System-Internal pipeline definition，仓库也无 pipeline YAML；本仓不能进入“全部 canonical repos 当前 SHA 已过 CI”的表述。

## 20. Cyrene-Reactor 详查

### 20.1 Hybrid 仍是活动旧策略

[`engine_factory.py:136`](../../Cyrene-Services/Cyrene-Reactor/runtime/core/src/cy_exec/engines/engine_factory.py) 仍注册旧别名 `nvidia`、`cuda`、`ascend`、`npu`，并在 165–170 行注册 `hybrid`。Pro [`engine_selector.py:100`](../../Cyrene-Services/Cyrene-Reactor/runtime/pro/src/cy_exec_pro/core/engine_selector.py) 也注册 Hybrid。

[`hybrid_engine.py:50`](../../Cyrene-Services/Cyrene-Reactor/runtime/core/src/cy_exec/engines/hybrid_engine.py) 顺序尝试 NVIDIA/Ascend，并对任何 `Exception` 继续下一个 backend。这不是跨设备拆分，只是宽异常 fallback；模型损坏、参数错误、权限错误和真实硬件不兼容都会被同样吞掉。它应从默认 registry 移除，或只对明确的“backend unavailable”错误执行有审计的策略切换。

### 20.2 具体 engine owner 尚未收敛

Reactor README 把 TensorRT-LLM 声明为 canonical runtime component，`engine_factory.py:114-120` 与 Pro selector 都主动注册 [`trt_engine.py`](../../Cyrene-Services/Cyrene-Reactor/runtime/core/src/cy_exec/engines/trt_engine.py)。同一仓 [`architecture-and-lifecycle.md:59-62`](../../Cyrene-Services/Cyrene-Reactor/docs/architecture-and-lifecycle.md) 又把 TensorRT、vLLM、Ascend 列为待抽取的 replaceable plugins，而 Plugins 已有 `cyrene.engines.tensorrt-llm`。

这里需要一次明确的 owner 决策，不能继续同时维护 Product 内置 engine 与 Official plugin 两个实现。建议 Reactor 保留 Deployment/Serving lifecycle 与选择策略，具体 backend implementation 由 capability binding 注入。

### 20.3 模拟 Pro 与 legacy sidecar

- [`hot_swapper.py:1-21`](../../Cyrene-Services/Cyrene-Reactor/runtime/pro/src/cy_exec_pro/engines/lora/hot_swapper.py) 明确只模拟 LoRA load/unload/switch，却仍更新 `loaded=True`、成功次数和耗时；只有调用方主动检查 `BACKEND_INTEGRATION_SIMULATED` 才能避免误判。
- `runtime/pro/README.md` 明确 non-stream UDS token count 是 placeholder，无 Rust scheduler 时 fallback 不是 production scheduler。
- [`architecture-and-lifecycle.md:18`](../../Cyrene-Services/Cyrene-Reactor/docs/architecture-and-lifecycle.md) 把 sidecar 标为 `Legacy gRPC proxy prototype`；其 telemetry reporter 把 queue/KV/GPU 值写为 0、未向 coordinator 发送，client 收到的新 weight/capacity 也不应用。
- README 仍提到不存在的 `Backup/` 和“Pro training package remains in enterprise bundle”，需要验证后移到历史说明。

这些路径可以作为研发骨架保留，但 production profile 和 CI 账本必须显式排除 simulated success。

## 21. Cyrene-Yield 详查

### 21.1 主训练控制路径没有发现新的隐蔽旧 owner

`training/core` 当前把 Kernel 生产路径与 test-only `InProcessKernelPort` 分开，也有禁止旧 `AgentService/ExecuteCommandStream` 的 guard。扫描命中的 `NotImplementedError` 主要是接口 hook；没有发现新的根级 archive、缓存产物或未经登记的第二套 TrainingRun authority。

### 21.2 README 引用已经失效

[`training/core/README.md:7`](../../Cyrene-Services/Cyrene-Yield/training/core/README.md) 固定 Platform `27e69c46`，远落后于当前 `a402b7b8`，并链接不存在的 `docs/KERNEL_CANONICAL_BASE.md`。同一文件还记录 `KernelAuthority has no Configure RPC` 的 live envelope contract gap。应把固定 SHA 移入可机器验证的 lock/ledger，并让 README 指向真实存在的当前契约说明。

### 21.3 LLaMA Factory vendored fork 需要独立治理

[`plugins/llama-factory-training`](../../Cyrene-Services/Cyrene-Yield/plugins/llama-factory-training) 有 586 个跟踪文件、约 64,211 行 Python。根 README 写明来源为 `hiyouga/LLaMA-Factory` 与 `Icy-Lunar/LlamaFactory@f5a4a8f8`，但该目录没有独立 machine-readable upstream/provenance 文件或 patch queue；同时仍保留上游 API、eval、chat、v1 plugin 框架等大量非训练源码。

这不应作为普通死代码批量删除，因为它是实际训练 backend。需要先建立：upstream commit、许可证/通知、Cyrene patch 列表、允许保留的模块、禁用但仍跟踪的模块、更新流程和回归矩阵。否则 43 个 TODO、36 个空文件和上游大面积变更无法区分“上游占位”“Cyrene 删除后残壳”和“未来能力”。

Repository Policy 同时写明 Yield 不拥有 concrete training engine internals。若继续维护此 fork，应把它明确成受管理的 Official engine/plugin dependency；若 Yield 自己拥有 fork，就要修改边界声明。

## 22. Cyrene-Exchange 详查

### 22.1 删除了源码，但文档还在发布旧目录

当前 `git ls-files 'legacy/**'` 为 0；但 [`README.md:19`](../../Cyrene-Services/Cyrene-Exchange/README.md)、`docs/README.md`、`docs/architecture/overview.md`、`docs/API.md` 和 `docs/modules/README.md` 仍反复写 former Rust/Python gateway、migration、CLI 保存在 `legacy/`。这些是确定的 stale references，应立即修正。

### 22.2 Kotlin coordinator 仍拥有训练和脚本生命周期

`components/coordinator` 有 58 个文件，活动 main source 约 4,622 行。[`GrpcServerConfig.kt:38`](../../Cyrene-Services/Cyrene-Exchange/components/coordinator/src/main/kotlin/com/cy/llm/coordinator/config/GrpcServerConfig.kt) 同时注册 inference 与 training service；[`CoordinatorTrainingService.kt:63`](../../Cyrene-Services/Cyrene-Exchange/components/coordinator/src/main/kotlin/com/cy/llm/coordinator/grpc/CoordinatorTrainingService.kt) 管理训练队列，304 行起管理自定义脚本；[`WorkerGrpcClient.kt:100`](../../Cyrene-Services/Cyrene-Exchange/components/coordinator/src/main/kotlin/com/cy/llm/coordinator/worker/WorkerGrpcClient.kt) 直接调用 worker training/script RPC。

这与 Exchange Policy 的 gateway/routing owner 冲突，也与 Yield 的 TrainingRun authority、Platform 的受管执行边界重复。应把仍需的路由/worker health 语义收敛到 Product gateway seam，把训练与脚本服务迁出或删除。

### 22.3 其他漂移

- Policy 写有 Cargo build system，仓库没有 `Cargo.toml`；
- `WorkerGrpcClient` 的 coroutine `awaitClose` 块为空，没有显式取消底层 gRPC call，客户端取消后的资源释放需要补集成证明；
- `gateway.py` 仍允许旧 `token_validator` 生成固定的 `legacy-validator` / `legacy-workspace` principal。若该构造路径仍可由 production builder 使用，应设置 removal version；否则移到 test adapter。

## 23. Catalyst、Echo 与 Navigator 详查

### 23.1 Catalyst

当前没有跟踪 `legacy/`。但 [`README.md:15`](../../Cyrene-Services/Cyrene-Catalyst/README.md) 和 [`docs/API.md:95`](../../Cyrene-Services/Cyrene-Catalyst/docs/API.md) 仍称对话语料保存在 `legacy/CY_LLM_Training`；实际上该语料已移出仓库，`data/samples/.../README.md` 也明确说明了这一点。README/API 应改成当前 DatasetVersion/样例事实。

Policy 和 Workspace 都把 build systems 写空，尽管根 [`pyproject.toml`](../../Cyrene-Services/Cyrene-Catalyst/pyproject.toml) 可构建 `cyrene-catalyst` wheel。源代码未发现新的 TODO/NotImplemented 或第二套 DatasetVersion authority。

### 23.2 Echo

当前没有跟踪 `legacy/`。[`README.md:17`](../../Cyrene-Services/Cyrene-Echo/README.md) 与 [`docs/API.md:89`](../../Cyrene-Services/Cyrene-Echo/docs/API.md) 仍声称 Navigator feedback panel 位于 `legacy/navigator-feedback/`，但该目录已经由 closure commit 删除。Repository Policy 还把 owner 写成 `Audio & Realtime Team`、职责写成 `Realtime audio service definition and legacy assets`，与当前 model evaluation/feedback 实现明显不符。

Policy/Workspace 同样遗漏实际 pyproject/wheel。Echo 的真实 Exchange judge 仍诚实标为 `WIRED_NOT_RUN`，本轮没有把该凭据门槛升级为缺陷。

### 23.3 Navigator

当前没有跟踪 `legacy-dh/`，但 [`README.md:17`](../../Cyrene-Services/Cyrene-Navigator/README.md) 与 [`docs/API.md:11`](../../Cyrene-Services/Cyrene-Navigator/docs/API.md) 仍声称完整 Dh codebase 保存在该目录；Workspace `governance/databases.yaml` 还把它当三个数据库/Redis 的 container authority。

Windows 原生应用自己的 [`README.md:1-8`](../../Cyrene-Services/Cyrene-Navigator/apps/windows/README.md) 诚实说明它只有 mock 数据、没有 backend/Harness/Exchange/session persistence；但项目本身会构建可执行文件，且 [`MainWindow.cs:36-40`](../../Cyrene-Services/Cyrene-Navigator/apps/windows/src/Cyrene.Navigator.Windows/MainWindow.cs) 直接绑定 mock service。下一阶段要么把它放入显式 `prototype` profile，要么先注入接口并接真实 Session/Exchange，再进入 release artifact。

Navigator 根还有 Python Product package、3 个 Cargo manifest 和 2 个 C# project；仓库 Policy 只列 Cargo/NPM，Workspace 写空。未发现跟踪中的 `.navigator/proof`、缓存、数据库或构建产物；本地被 ignore 的证明/缓存目录不属于源码结论。

## 24. Workspace 自身需要清理的旧事实

除第 10 节已列的 Plugins 旧结论外，Workspace 还需要处理：

1. [`governance/databases.yaml:73`](../governance/databases.yaml) 以及 120、133、143 行仍指向删除的 Navigator `legacy-dh` compose；这些路径不再能启动任何开发数据库。
2. [`repositories.yaml:268`](../repositories.yaml) 起把 Catalyst、Echo、Navigator 的 build systems 留空，导致 workspace verifier 无法发现它们的 Python/Cargo/.NET/NPM gate。
3. [`governance/accepted-baseline.yaml`](../governance/accepted-baseline.yaml) 尚未记录本次第一次完整闭环的当前集合。
4. [`docs/PRODUCTS.md`](PRODUCTS.md) 仍把 Reactor TensorRT-LLM/HuggingFace fallback 写成已交付产品能力，应和 Reactor 的 real-GPU、plugin binding 证据状态对齐。
5. [`docs/cyrene-architecture-refactor-report.md`](plans/cyrene-architecture-refactor-report.md) 除 Plugins 过度表述外，也需要删除“Hybrid 已弃用、TensorRT 已完成插件化”这类与当前 Reactor registry 冲突的结论。

Workspace 当前 16 个源码文件中没有发现新的运行时 duplicate authority；主要债务集中在跨仓 truth projection，而不是 Workspace 自己的执行代码。

## 25. 第一次完整闭环 CI 的准确边界

Azure Hosted 当前成功记录如下：

| Repository | Build | Source | 结论 |
| --- | ---: | --- | --- |
| Workspace | [431](https://dev.azure.com/dohorizon/Cyrene/_build/results?buildId=431) | exact `main@2e53b7fc` | `succeeded` |
| Platform | [401](https://dev.azure.com/dohorizon/Cyrene/_build/results?buildId=401) | PR head `cd454002` | `succeeded`；tree 与 `develop@a402b7b8` 完全相同，但不是 exact merge SHA run |
| Plugins | [425](https://dev.azure.com/dohorizon/Cyrene/_build/results?buildId=425) | exact `develop@0b13720e` | `succeeded` |
| Reactor | [427](https://dev.azure.com/dohorizon/Cyrene/_build/results?buildId=427) | exact `develop@3b8d4e92` | `succeeded` |
| Yield | [388](https://dev.azure.com/dohorizon/Cyrene/_build/results?buildId=388) | exact `develop@c71057f6` | `succeeded` |
| Exchange | [428](https://dev.azure.com/dohorizon/Cyrene/_build/results?buildId=428) | exact `develop@3379bb4e` | `succeeded` |
| Catalyst | [432](https://dev.azure.com/dohorizon/Cyrene/_build/results?buildId=432) | exact `develop@cf568ae9` | `succeeded` |
| Echo | [426](https://dev.azure.com/dohorizon/Cyrene/_build/results?buildId=426) | exact `develop@8f51170e` | `succeeded` |
| Navigator | [433](https://dev.azure.com/dohorizon/Cyrene/_build/results?buildId=433) | exact `develop@41a42e86` | `succeeded` |

Astrbot-Rev 当前 exact `develop@8787db7a` 有 GitHub Actions `CI` run 33997906381 成功。DH-System-Internal 没有当前 pipeline definition 或 run。因此，当前最准确的项目状态是：**核心产品闭环和大多数规范分支已经取得 Hosted CI 基线；还没有“11 个 canonical repository 的 exact current head 全部通过 CI”。**

同一批当前 SHA 的 GitHub Actions 在 Workspace、Platform、Plugins、Reactor、Yield、Exchange、Catalyst、Echo、Navigator 上都显示 failure，但 job annotation 一致为 recent account payments failed / spending limit needs to be increased，且任务在 1–13 秒内、0 steps 结束。这是账户/CI 环境阻塞，不是源码测试失败。

上述 CI 成功仍不能证明：

- Reactor/Plugins TensorRT、完整 LoRA hot swap、真实多 backend Hybrid；
- Yield 真实 CUDA LLaMA Factory 训练、checkpoint resume 和完整 DatasetVersion v1→v2 生命周期；
- Echo 真实凭据的 Exchange LLM judge；
- DH 真企微、真数据库与云 provider；
- Navigator Windows mock 原型的真实 Session/Exchange 接线；
- 被 `#[ignore]`、skipped 或只跑模拟实现的路径。

## 26. 下一阶段开发前的建议清理门

建议把清理拆成下面五个可验收批次，完成后再更新 accepted baseline：

1. **Truth gate**：统一 DH 分支 authority；修 9 个 GitHub repo 的 visibility/default-branch 元数据；补齐 build systems；清除所有已删除路径和过期状态表。
2. **Fail-closed gate**：修 DH 模型/云连接器和 Plugins mock 默认成功；Reactor simulated Pro 路径从 production profile 隔离；所有结果带 `REAL` / `SIMULATED` / `NOT_RUN` 状态。
3. **Ownership gate**：Exchange 移出训练/custom-script service；Reactor 与 Plugins 确定 concrete engine 唯一 owner；Custom Script 回到 Platform 受管执行。
4. **Compatibility gate**：给 Astrbot `python-compat`、Platform infrastructure compatibility、Plugins 11 组兼容树分别建立使用者、最后支持版本、removal gate 和调用量证据；合并跨仓重复 renderer/Shiki 资产。
5. **Acceptance gate**：补 DH pipeline；对所有 canonical exact heads 重跑 source CI；把真实 GPU、真实凭据、Windows 原生构建与被跳过的环境测试另列，不用 unit/mock success 代替。

每个批次应以“小范围删除/迁移 + 对应 guard + 当前 exact SHA CI”交付。不要把大规模格式化、兼容树删除和 owner 迁移混在一个 PR 中，否则很难证明行为没有回退。

## 27. 扩展审计中的阴性结果与限制

- Platform、Reactor、Exchange、Catalyst、Echo、Navigator 的规范分支没有跟踪中的根级 `legacy/` / `archive/` / `backup/`；Astrbot 的旧面位于正常目录并由显式 `python-compat` target 暴露；Yield 的大体积第三方面位于登记的 vendored plugin 目录。
- 除已列出的 renderer 与 Shiki runtime，未发现跨仓完全相同且超过 20 行的其他源码 blob。
- 没有发现 canonical Git 树跟踪 `.venv`、pytest/mypy/ruff cache、`__pycache__`、SQLite/WAL、log 或 `.navigator/proof`。本地 checkout 中存在的 ignored cache/proof 不计入仓库源码。
- Catalyst 主源码、Echo 主源码、Yield Product controller 没有出现新的明显空实现；其主要问题分别是文档/元数据、凭据验收边界、vendored backend 治理。
- 本轮是静态审计、构建/CI 证据回读和已有测试账本复核，没有执行真实 GPU、企微、外部模型 API、Windows UI 或生产部署。
- 报告只覆盖第 14 节列出的远端提交，不覆盖未合并 PR、历史分支和 ignored 本地文件。
