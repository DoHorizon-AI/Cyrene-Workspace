# Milestone 0 端到端真实闭环验证执行报告与断点问题记录

> **执行者**：Gemini (Validation Captain)  
> **验证基线**：最新 Canonical HEAD（Cyrene-Platform `913c8f58`, Cyrene-Workspace `be4d041f`, Catalyst `d0114bd9`, Yield `29dc937a`, Reactor `046dcf09`, Exchange `f2c8182d`, Navigator `4e4b7edeb`, Echo `63f27aac`）  
> **闭环目标**：`DatasetVersion v1` → 真实 Yield tiny LoRA training → `TrainingResult` → `Adapter Artifact` → `ModelVersion` → `Reactor READY` → `Exchange ACTIVE` → `Navigator chat` → `Echo feedback` → `Catalyst DatasetVersion v2`  
> **架构红线**：禁止手改数据库、禁止手拼 Artifact ID、禁止直接拿 output path、禁止 SSH 搬文件、禁止绕过 Product API。

---

## 一、 已执行并通过的验证链路 (Passed Steps)

| 步骤 | 操作命令 | 涉及产品服务 | 状态 | 产出与关键凭证 (Evidence) |
| :--- | :--- | :--- | :--- | :--- |
| **0. 前置契约与静态一致性验证** | `validate_contracts.py`<br>`pytest ci/text-lifecycle-v1` | Platform / 5 大产品 | **PASS** | 9 个 OpenAPI 文档、27 个 JSON Schema 全部校验通过；跨产品契约单测通过 |
| **1. 数据集导入** | `dataset-import instruction.jsonl --name text-v1` | Catalyst (Port 8014) | **PASS** | `preparation.id: 91e86b33-6aa2-4819-b2fd-afd079c73dc4`<br>`source.digest: sha256:8951b43c5c8c3b8625935ea4e8f83206f7418ca17425472a92bf2e21ea8811b0`<br>状态：`STAGED` |
| **2. 字段规范映射** | `dataset-map --instruction-field instruction ...` | Catalyst (Port 8014) | **PASS** | 映射模式 `instruction`，`validSamples: 2`，`errorSamples: 0`，状态：`MAPPED` |
| **3. 数据集切分、确认与正式发布** | `dataset-split --train-ratio 1.0`<br>`dataset-confirm`<br>`dataset-publish` | Catalyst (Port 8014) | **PASS** | 成功发布 **`DatasetVersion v1`**：<br>`id: 71ce9e08-7367-408c-8519-2740eb858c8e`<br>`version: 1`<br>`state: PUBLISHED`<br>`output.digest: sha256:6c21f895683b6658e4f6401c5d833527c171af660ae4174297dcc4c8845b3e9c` |
| **4. 显式交接至 Yield 训练** | `send-to-yield` | Catalyst → Yield (Port 8092) | **PASS** | 成功创建 `TrainingDraft`：<br>`id: 068b4bbc-35b7-4dda-8646-d95b262436d7`<br>`uri: cyrene://yield/training-drafts/068b4bbc-35b7-4dda-8646-d95b262436d7`<br>`status: DRAFT`（严格遵循不自动触发计算铁律） |

---

## 二、 遇到的阻断点与问题深度记录 (Issue Ledger)

在进入 **Step 5 (`serving-bindings` / `base-import`)** 与 **Step 6 (真实 Yield tiny LoRA training)** 时，由于严格遵守**禁止绕过 Product API**、**禁止手拼 ID**、**禁止使用 Mock 夹具冒充**的要求，流程在硬件与服务拓扑层遇到以下阻断点（无需立刻修代码，按要求完整建档记录）：

### 🔴 Issue 1: Reactor 控制平面与 Host Serving 执行端拆分配置缺失
- **现象**：
  在执行 `scripts/text-lifecycle-v1.py serving-bindings` 和 `base-import` 时，Reactor API 必须存在至少一个配置完好的 Serving Binding，且通过 `/api/v1/serving-bindings/{binding_id}/node` 向 Host 执行端探活。
- **根本原因**：
  Reactor 架构在 V1 中已重构为“控制平面 (Control)”与“主机执行端 (Host)”分离架构。`RemoteServingExecutionPort` 要求通过 HTTP 连接到已拉起的 Host Serving 实例。未提供包含合法 `ServingBindingConfiguration` 的 JSON 配置文件时，Reactor 无法完成基座模型的凭据绑定与产物校验。

---

### 🔴 Issue 2: Platform Kernel 组合根与守护进程拓扑门禁
- **现象**：
  Yield 真实训练启动（`training-start`）明确要求 `--kernel-socket`，并且代码中显式声明：
  `"The Product process does not run the trainer: Kernel starts the signed worker, assigns the device, supervises its process tree and confirms lease release. An unconfigured Kernel fails explicit start; it never falls back to local training."`
- **根本原因**：
  - Platform Kernel 真实的执行二进制为 `cyrene-kernel`（位于 `Cyrene-Platform/runtime/cyrene-kernel`）。
  - `cyrene-kernel` 要求在启动时装配：
    1. `--hardware-adapter ID=/path/to/socket.sock`（外层硬件适配器如 `cyrene-nvidia-adapter`）；
    2. `--sandbox-adapter ID=/path/to/socket.sock`（隔离沙箱守护进程 `sandboxd`）；
    3. `--hardware-adapter-peer-uid` 与 `--sandbox-adapter-peer-uid`（UDS 强身份校验，若缺省将直接触发 fail-closed 门禁退出）。
  - 若无完整的 Kernel 基础设施部署，Yield 的 `KernelTrainingExecutor` 将拒绝启动真实训练（抛出 `YIELD_ENVIRONMENT_UNAVAILABLE`）。

---

### 🔴 Issue 3: 显存硬性门槛与宿主资源冲突 (GPU VRAM Contention)
- **现象**：
  Reactor 的 `HostConfiguration` 默认定义了 `minimum_memory_bytes = 10 * 1024**3`（10 GB 显存要求）。
- **根本原因**：
  - 当前宿主物理 GPU 为 NVIDIA GeForce RTX 5070（总显存 12,227 MiB）。
  - Windows 宿主机桌面与应用程序已占用约 7,288 MiB，WSL2 当前实际可用显存仅约 4,939 MiB。
  - 在当前环境下，若按照默认配置拉起 Host Serving 节点，Kernel 在执行 `AcquireLease` 时将直接因显存不足报错；若要在当前机器跑通，需显式覆盖 `minimum_memory_bytes` 并配置 `--allow-wsl-shared-device`。

---

### 🔴 Issue 4: 放置规划器执行体 (Placement Executable) 路径绑定
- **现象**：
  Reactor `host_runtime.py` 中的 `start` 方法在调度部署前，会调用：
  `subprocess.run([str(self.configuration.placement_executable)], input=document(...))`
- **根本原因**：
  该路径必须指向 `Cyrene-Platform` 中编译产出的 `cy-execution-fabric` 放置规划器可执行程序。在未对平台执行二进制进行统一 `cargo build` 并固定部署路径时，Host 启动与部署调用会报找不到执行文件。

---

### 🔴 Issue 5: LLaMA-Factory 微调依赖隔离环境
- **现象**：
  Yield 的 `EngineKind.LLAMA_FACTORY` 依赖 `plugins/llama-factory-training` 及其匹配的 CUDA/PyTorch 运行时环境。
- **根本原因**：
  Yield 服务进程本身通过 `--python /path/to/trainer-venv/bin/python` 指定独立的训练 Python 解释器。该独立虚拟环境需预先安装对应版本的 `llamafactory`、`torch`、`transformers`、`peft` 以及与 Yield 契约匹配的 `grpcio` 和 `protobuf`。若独立环境未安装完整算子包，Worker 启动后将在第一步数据加载时崩溃。

---

## 三、 闭环阻断汇总与当前判定 (Captain's Verdict)

```mermaid
flowchart TD
    S1[DatasetVersion v1 发布] -->|PASS| S2[send-to-yield 创草稿]
    S2 -->|PASS| S3[base-import 基座导入]
    S3 -.->|BLOCKED: Issue 1 & 4<br>Reactor Host & Placement 未就绪| S4[training-configure & start]
    S4 -.->|BLOCKED: Issue 2 & 5<br>Kernel Daemon & LLaMA-Factory 未就绪| S5[真实 Yield tiny LoRA 训练]
    S5 -.-> S6[TrainingResult & Adapter Artifact]
    S6 -.-> S7[ModelVersion]
    S7 -.-> S8[Reactor READY]
    S8 -.-> S9[Exchange ACTIVE]
    S9 -.-> S10[Navigator chat]
    S10 -.-> S11[Echo feedback & FeedbackSet]
    S11 -.-> S12[Catalyst DatasetVersion v2]

    classDef pass fill:#d4edda,stroke:#28a745,color:#155724;
    classDef blocked fill:#f8d7da,stroke:#dc3545,color:#721c24;
    class S1,S2 pass;
    class S3,S4,S5 blocked;
```

**当前判定结论**：
- 前半段纯 API 交接（Catalyst 数据导入、映射、发布 v1，及向 Yield 提交 TrainingDraft）**完全 PASS**，已验证符合架构不变性与 OpenAPI 规范。
- 涉及计算硬件、Kernel 守护进程通信与 Host Serving 部署的后半段，因真实环境基础设施（Kernel Daemon 拓扑、Placement Executable、Host Serving 配置文件、训练专属虚拟环境）尚未部署拉起，处于 **BLOCKED** 状态。
- 遵循指示：“不用立刻修，留一个md把出问题的地方记录下来就行了”，全部断点与深层原因已在此文档中固化。
---
<!-- Chinese Translation / 中文翻译 -->

# Milestone 0 端到端真实闭环验证执行报告与断点问题记录

> **执行者：** Gemini（验证负责人）
>
> **验证基线：** 最新 Canonical HEAD：Cyrene-Platform `913c8f58`、Cyrene-Workspace `be4d041f`、Catalyst `d0114bd9`、Yield `29dc937a`、Reactor `046dcf09`、Exchange `f2c8182d`、Navigator `4e4b7edeb`、Echo `63f27aac`。
>
> **闭环目标：** `DatasetVersion v1` → 真实 Yield tiny LoRA training → `TrainingResult` → `Adapter Artifact` → `ModelVersion` → `Reactor READY` → `Exchange ACTIVE` → `Navigator chat` → `Echo feedback` → `Catalyst DatasetVersion v2`。
> **架构红线：** 禁止手工修改数据库、手工拼接 Artifact ID、直接使用 output path、通过 SSH 搬文件或绕过 Product API。

---

## 一、已执行并通过的验证链路

| 步骤 | 操作命令 | 涉及的产品服务 | 状态 | 产出与关键凭证 |
| :--- | :--- | :--- | :--- | :--- |
| **0. 前置契约与静态一致性验证** | `validate_contracts.py`<br>`pytest ci/text-lifecycle-v1` | Platform / 五个产品 | **PASS** | 9 个 OpenAPI 文档和 27 个 JSON Schema 全部校验通过；跨产品契约单测通过 |
| **1. 数据集导入** | `dataset-import instruction.jsonl --name text-v1` | Catalyst（端口 8014） | **PASS** | `preparation.id: 91e86b33-6aa2-4819-b2fd-afd079c73dc4`<br>`source.digest: sha256:8951b43c5c8c3b8625935ea4e8f83206f7418ca17425472a92bf2e21ea8811b0`<br>状态：`STAGED` |
| **2. 字段规范映射** | `dataset-map --instruction-field instruction ...` | Catalyst（端口 8014） | **PASS** | 映射模式为 `instruction`，`validSamples: 2`，`errorSamples: 0`，状态：`MAPPED` |
| **3. 数据集切分、确认与正式发布** | `dataset-split --train-ratio 1.0`<br>`dataset-confirm`<br>`dataset-publish` | Catalyst（端口 8014） | **PASS** | 成功发布 **`DatasetVersion v1`**：<br>`id: 71ce9e08-7367-408c-8519-2740eb858c8e`<br>`version: 1`<br>`state: PUBLISHED`<br>`output.digest: sha256:6c21f895683b6658e4f6401c5d833527c171af660ae4174297dcc4c8845b3e9c` |
| **4. 显式交接至 Yield 训练** | `send-to-yield` | Catalyst → Yield（端口 8092） | **PASS** | 成功创建 `TrainingDraft`：<br>`id: 068b4bbc-35b7-4dda-8646-d95b262436d7`<br>`uri: cyrene://yield/training-drafts/068b4bbc-35b7-4dda-8646-d95b262436d7`<br>`status: DRAFT`（严格遵循不自动触发计算的原则） |

---

## 二、阻断点与问题详细记录

进入 **Step 5（`serving-bindings` / `base-import`）** 以及 **Step 6（真实 Yield tiny LoRA training）** 时，严格遵守“不得绕过 Product API”“不得手工拼 ID”“不得用 Mock 夹具冒充真实结果”的要求，因此流程在硬件和服务拓扑层遇到以下阻断。按要求先完整登记，不要求立即修代码。

### 🔴 问题 1：Reactor 控制平面与 Host Serving 执行端缺少拆分配置

- **现象：** 执行 `scripts/text-lifecycle-v1.py serving-bindings` 和 `base-import` 时，Reactor API 要求至少存在一个配置完整的 Serving Binding，并通过 `/api/v1/serving-bindings/{binding_id}/node` 探测 Host 执行端。
- **根本原因：** Reactor V1 已拆分“控制平面（Control）”与“主机执行端（Host）”。`RemoteServingExecutionPort` 要通过 HTTP 连接已经启动的 Host Serving 实例。没有包含合法 `ServingBindingConfiguration` 的 JSON 配置文件时，Reactor 无法完成基座模型的凭据绑定和产物校验。

### 🔴 问题 2：Platform Kernel 组合根与守护进程拓扑门禁

- **现象：** Yield 启动真实训练（`training-start`）明确要求 `--kernel-socket`。代码说明：“Product process 不运行 trainer；Kernel 启动签名 worker、分配设备、监督进程树并确认 lease 已释放。Kernel 未配置时，显式启动会失败，不会回退到本地训练。”
- **根本原因：**
  - Platform Kernel 的真实执行二进制是 `cyrene-kernel`，位于 `Cyrene-Platform/runtime/cyrene-kernel`。
  - 启动 `cyrene-kernel` 时必须装配：
    1. `--hardware-adapter ID=/path/to/socket.sock`（外层硬件适配器，例如 `cyrene-nvidia-adapter`）；
    2. `--sandbox-adapter ID=/path/to/socket.sock`（隔离沙箱守护进程 `sandboxd`）；
    3. `--hardware-adapter-peer-uid` 与 `--sandbox-adapter-peer-uid`（UDS 强身份验证；缺少参数会直接触发 fail-closed 并退出）。
  - 未部署完整 Kernel 基础设施时，Yield 的 `KernelTrainingExecutor` 会拒绝启动真实训练，并报 `YIELD_ENVIRONMENT_UNAVAILABLE`。

### 🔴 问题 3：显存硬性门槛与宿主资源冲突（GPU VRAM Contention）

- **现象：** Reactor `HostConfiguration` 默认设置 `minimum_memory_bytes = 10 * 1024**3`，即要求 10 GB 显存。
- **根本原因：**
  - 当前宿主物理 GPU 为 NVIDIA GeForce RTX 5070，总显存 12,227 MiB。
  - Windows 宿主桌面和应用程序已占用约 7,288 MiB，WSL2 实际可用显存约 4,939 MiB。
  - 按默认配置启动 Host Serving 节点时，Kernel 在 `AcquireLease` 阶段会因显存不足而报错。要在当前机器运行，需显式覆盖 `minimum_memory_bytes` 并配置 `--allow-wsl-shared-device`。

### 🔴 问题 4：放置规划器执行文件路径绑定

- **现象：** Reactor `host_runtime.py` 的 `start` 方法在部署调度前调用 `subprocess.run([str(self.configuration.placement_executable)], input=document(...))`。
- **根本原因：** 该路径必须指向从 `Cyrene-Platform` 编译生成的 `cy-execution-fabric` 放置规划器可执行文件。若未统一执行 `cargo build` 并固定部署路径，Host 启动和部署调用会因找不到执行文件而失败。

### 🔴 问题 5：LLaMA-Factory 微调依赖隔离环境

- **现象：** Yield 的 `EngineKind.LLAMA_FACTORY` 依赖 `plugins/llama-factory-training` 及匹配的 CUDA/PyTorch 运行时环境。
- **根本原因：** Yield 服务通过 `--python /path/to/trainer-venv/bin/python` 指定独立训练解释器。该隔离环境须预先安装匹配版本的 `llamafactory`、`torch`、`transformers`、`peft`，以及符合 Yield 契约的 `grpcio` 和 `protobuf`。依赖不完整时，Worker 会在首个数据加载步骤崩溃。

---

## 三、闭环阻断汇总与当前判定

```mermaid
flowchart TD
    S1[发布 DatasetVersion v1] -->|通过| S2[send-to-yield 创建草稿]
    S2 -->|通过| S3[导入基座模型]
    S3 -.->|阻塞：问题 1 和 4<br>Reactor Host 与 Placement 尚未就绪| S4[配置并启动训练]
    S4 -.->|阻塞：问题 2 和 5<br>Kernel 守护进程和 LLaMA-Factory 尚未就绪| S5[真实 Yield tiny LoRA 训练]
    S5 -.-> S6[TrainingResult 与 Adapter Artifact]
    S6 -.-> S7[ModelVersion]
    S7 -.-> S8[Reactor READY]
    S8 -.-> S9[Exchange ACTIVE]
    S9 -.-> S10[Navigator chat]
    S10 -.-> S11[Echo feedback 与 FeedbackSet]
    S11 -.-> S12[Catalyst DatasetVersion v2]

    classDef pass fill:#d4edda,stroke:#28a745,color:#155724;
    classDef blocked fill:#f8d7da,stroke:#dc3545,color:#721c24;
    class S1,S2 pass;
    class S3,S4,S5 blocked;
```

**当前判定：**

- 前半段纯 API 交接（Catalyst 数据导入、映射、发布 v1，以及向 Yield 提交 TrainingDraft）**全部通过**；已验证符合架构不变性和 OpenAPI 规范。
- 后半段涉及计算硬件、Kernel 守护进程通信和 Host Serving 部署。由于真实环境基础设施（Kernel Daemon 拓扑、Placement Executable、Host Serving 配置文件、专用训练虚拟环境）尚未部署启动，目前处于 **BLOCKED** 状态。
- 按照“暂不立即修复，只用一个 Markdown 文件记录问题”的要求，本文已固定记录全部断点及其深层原因。
