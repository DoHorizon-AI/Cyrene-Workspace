---
name: cyrene-plugin-development
description: 开发、设计或审查 Cyrene 通用插件及其能力契约、运行时、节点、Navigator 监控、日志和 MCP 集成。用于 Cyrene 多仓库插件生态；不用于 Codex 插件打包或无关应用插件。
---

# Cyrene 通用插件开发

目标是让同一插件能力能被所属 Product 使用，并按需通过 Client 的节点、监控页面和 MCP 操作。插件不必具有所有界面；一个包可以提供多种能力或贡献。用户已明确要求后续开发同时考虑 MCP、Navigator 和节点，并让平台日志可由插件扩展。

## 先定位真实契约

读取任务所涉 checkout 的 AGENTS.md、HEAD、工作树状态和实现入口。Workspace 的 repositories.yaml 用于找仓库，不作为当前接口 schema 的替代。Client 曾名 Studio，本机目录名不保证等于远端名；Navigator Product 与 Client 的 Navigator 监控面板是不同模块。

本 skill 的接口清单是本地源码快照，不是部署清单。能力声明、实现、宿主注册、UI/MCP 暴露、实际联调、发布分别核验。禁止依据工具名、UI 按钮或测试文件存在推断生产可用。按任务需要读取：

| 任务 | 参考 |
| --- | --- |
| 查接口所有者、跨仓调用路径、现有能力和缺口 | [system-interfaces.md](references/system-interfaces.md) |
| 编写 manifest、选择运行方式、配置/状态/制品及生命周期 | [plugin-contracts.md](references/plugin-contracts.md) |
| 接入节点、MCP、Navigator、日志或扩展页面 | [workbench-contributions.md](references/workbench-contributions.md) |
| 审查完整性、选回归、迁移/发布和更新证据 | [delivery-checklist.md](references/delivery-checklist.md) |

需要逐项定位时查 [interface-inventory.json](references/interface-inventory.json)，按 repository/kind/source 筛选，避免整份灌入上下文。清单更新脚本和覆盖限制见 system-interfaces。

## 不混淆五个维度

- **能力**：如 dataset.preparation.v1、model.provider.v1，定义业务输入输出。
- **贡献**：node、监控视图、日志来源、用户命令、MCP 投影等接入方式；其中部分仍是设计方向，不是现成 manifest 字段。
- **执行放置**：Plugins manifest 的 inline / worker / service。
- **节点执行语义**：Client 的 reference / task / service / external；与上一项不能按字符串互相映射。
- **实现与发布**：Python/Rust/.NET/Java 等运行时、插件包版本、接口版本、节点类型版本和镜像摘要，各有独立用途。

## 开发流程

1. 明确能力所有者及调用方，选择已有版本化契约；新增通用资源/监管协议归 Platform，业务对象归 Product，可替换能力归 Plugins，图/交互/MCP 聚合归 Client。不要为了界面集成重写另一套 Kernel 或 Product。
2. 针对任务填写最小能力说明：稳定 ID/版本，输入输出与错误，作用域及权限，副作用，状态/制品，调用/停止/恢复语义，依赖，拟提供的 node/MCP/监控贡献。不适用项说明理由即可，不强制无 UI 插件做页面。
3. 先实现或复用 owner 的应用接口，再接 UI 和 MCP。同一操作共用验证、权限、版本、幂等及状态查询。异步操作保留 run/attempt/task/resource 身份，迟到结果不能覆盖新编辑。
4. 逐项核对贡献能否被发现、调用、观察和清理。声明式节点包可以注册不代表其 adapter、业务凭据、页面加载器或 MCP tool 已连接；未连接时明确不可用。
5. 验证实际业务后果和故障恢复，再更新文档、版本与证据。只改文档不必跑全系统/GPU 测试；真实云操作、发送消息和发布仍按当前用户授权处理，skill 不扩大授权。

## 三项必须评估的接入面

**MCP**：每项对用户/AI 开放的业务能力评估 tool/resource/prompt 的适用性；可调用功能必须有实际注册、schema、权限与状态读取，不能只加名字。不机械把全部内部方法、密钥管理、宿主管理和原始 shell 暴露给模型。当前共享服务工具与 Plugins 的 tool.provider.v1 / DirectPluginRuntime 不是同一协议，需要显式适配。

**节点**：适合工作流的能力声明稳定 type/typeVersion、类型化端口、参数、所属 Product、执行适配器、固定版本/镜像与依赖。引用节点不申请算力，长期服务不是一次性任务；图校验、预检与实际启动分开。布局沿用业务文档与 pinned 规则，不依赖 LiteGraph 内部 ID。

**Navigator 与日志**：节点/流程相关贡献按实际上下文出现；平台级服务日志可以独立存在。日志来源与页面 renderer 解耦，优先共用标准日志视图、授权数据接口和 MCP 查询。支持作用域筛选、游标恢复、分页/背压和卸载清理；保留历史来源。当前通用日志提供方与第三方页面宿主尚未实现，不能把拟议接口记作现有 API。

## 共同约束

- Platform 管资源、准入、Lease/Fence、实例与监管；Product 管业务任务/结果；Plugins 提供能力；Client 管流程与交互。日志文本不能作为训练成功、资源释放或业务终态的权威。
- PluginManifest 使用 Plugins 仓库真实 schema，Client 节点包使用 Client packageSchema；两份声明以明确引用关联，不能互相替代，验证器接受未知字段也不代表宿主支持。
- 凭据按宿主支持的 secret reference 注入；配置、用户参数、运行状态、幂等回执、日志和制品分别管理。不得将秘密放入图、模型上下文或浏览器持久存储。
- 停用、升级、崩溃和卸载明确处理在途任务、引用、订阅及数据保留；未知执行先核对原实例，不盲目重跑或悄悄换机器。模型建议、请求已接受和业务完成不是同一事实。
- 版本化契约与客户端兼容性要可追溯；保留老节点文档和未知类型，不以目录刷新静默改写正在运行的快照。

## 维护

重大插件契约、宿主扩展、权限、运行生命周期或 UI/MCP/监控变更后更新对应参考；从“拟议”转为“实现”必须给出真实入口与验证证据。源码快照记录 checkout HEAD、未提交状态与源文件摘要，不把旧日期刷新成新验收。

修改本 skill 后运行 skill-creator 的 quick_validate.py 并检查相对链接；更新扫描脚本时用实际 checkout 验证抽取结果、关键已知接口和失败报告。文档更新不得顺便修改业务实现、同步镜像规范、公开仓 source-manifest 或自动发布。
