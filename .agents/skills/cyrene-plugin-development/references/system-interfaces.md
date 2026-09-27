# 系统接口导航与盘点

观察日期：2026-09-27。范围为10个本地 checkout 的工作树，不是远端最新状态或运行部署；本次没有 fetch、启动业务服务或重跑各 Product 验收。机器索引见 [interface-inventory.json](interface-inventory.json)。

## 证据范围

逐项记录公开契约/源码路由声明的所属仓库、文件、适用行号、名称/方法、HEAD、工作树改动数和文件 SHA-256。索引还包括 MCP资源/提示、声明式 schema、能力目录、插件清单、C ABI 头与 Rust/TypeScript 接口入口。

它不是整个系统所有函数列表，也不是运行时注册表：动态挂载、反向代理、配置条件、私有服务凭据、编译特性及版本兼容仍要沿调用路径核对。不同类别不可相加当成唯一 API 数量；Platform proto 有源码镜像，部分 Product 保存其它 Product 的契约副本。Rust pub(crate) trait 已保留 visibility 标记，不代表插件可用公共 API。

| 仓库 | HEAD（短） | 工作树改动项 | 主要声明索引 |
| --- | --- | ---: | --- |
| Workspace | `ff7508c911df` | 15 | json_schema: 4 |
| Platform | `b8c8e0bee669` | 0 | rust_trait_declaration: 37, proto_contract: 30, proto_rpc: 120, json_schema: 5 |
| Plugins | `ab7be45a2c28` | 0 | c_abi_header: 3, capability_catalog: 14, json_schema: 18, proto_contract: 7, proto_rpc: 3, plugin_manifest: 15, rust_trait_declaration: 10 |
| Client | `b6727ef577e8` | 61 | client_http_group: 6, client_http_route: 20, mcp_resource: 2, mcp_prompt: 1, typescript_interface: 16, client_command: 43 |
| Yield | `a673e9902ad3` | 0 | json_schema: 6, openapi_operation: 11, python_route_declaration: 28 |
| Echo | `8887c0ca7112` | 0 | json_schema: 9, openapi_operation: 24, python_route_declaration: 31 |
| Catalyst | `6b67205febe7` | 0 | json_schema: 6, openapi_operation: 30, python_route_declaration: 22 |
| Reactor | `5cc2deb1aa5a` | 0 | json_schema: 5, openapi_operation: 21, python_route_declaration: 24, proto_contract: 1, proto_rpc: 3 |
| Exchange | `1061a59b0bdb` | 0 | json_schema: 3, openapi_operation: 15, python_route_declaration: 38 |
| Navigator | `7d8c1e79ffd6` | 0 | json_schema: 2, openapi_operation: 12, python_route_declaration: 27 |

本次优先采用已有整合 worktree 的 Platform/Client/Yield/Echo；其它仓库按 Workspace 拓扑定位。仓库绝对路径不写入可移植接口规范。所有自动扫描的 scan_errors 为空，只表示解析器覆盖内未发生错误，不等于无遗漏。Client 当前未提交变更必须结合 source_hashes；单靠 HEAD 无法复现新增 MCP/监控功能。

## 所有者与主调用路径

| 层 | 权威与接入点 | 插件开发应遵守 |
| --- | --- | --- |
| Workspace | repositories.yaml、治理和构建映射 | 拓扑不是业务接口；旧示例可能落后于真实 schema |
| Platform | contracts/proto/cyrene/core、provider、workspace；Kernel与Host/Runtime Agent | 资源/Lease/Fence、实例与监管；不消费插件业务 payload |
| Plugins | manifests、contracts/capabilities.yaml、能力 proto/JSON/C ABI、SDK/runtime | 能力输入输出、协议和可替换实现 |
| Product | 各服务的 api.py / OpenAPI / domain/service | 数据、训练、评估、部署、路由、Agent会话的业务权威 |
| Client | apps/control、packages/*-control、node-registry、monitoring | 图与排版、应用命令、运行协调、UI/MCP和只读监控聚合 |

主路径：UI/MCP → Client 应用服务 → 所属 Product；Product 通过 Platform 取得已授权 endpoint/binding 后，按版本化能力契约直连 Plugin。内置 Client图/服务器登记等操作无需假装经过某个 Product。日志/监控数据由实际 owner 提供，不能从展示层反向创造资源或终态。

Navigator 名称要拆开：Cyrene-Navigator Product 管 Agent/Harness持久化及现有 Web Host；Client:apps/web/src/monitoring 是工作台 Navigator；apps/web/services/navigator/src 是迁入 Client 的业务管理组件。

## Client 现有接口

以下43项是命令定义全集；每个 actor 实际 tools/list 还按权限和只读配置过滤。命令 schema 在对应 packages 路径，MCP 输入写操作另有 idempotencyKey。

| 命令组 | 来源 | 已定义命令 |
| --- | --- | --- |
| build-control | `packages/build-control/contracts.ts` | `builds.list_profiles`, `builds.preview`, `builds.start`, `builds.list`, `builds.get`, `builds.cancel`, `builds.read_events` |
| monitoring | `packages/monitoring/contracts.ts` | `monitoring.snapshot` |
| node-registry | `packages/node-registry/commands.ts` | `catalog.list_packages`, `catalog.preview_activation`, `catalog.activate` |
| pipeline-control | `packages/pipeline-control/contracts.ts` | `pipelines.compile`, `nodes.list_types`, `pipelines.list`, `pipelines.get`, `pipelines.create`, `pipelines.save`, `pipelines.patch`, `pipelines.layout`, `pipelines.preview_layout`, `pipelines.validate`, `pipelines.history`, `pipelines.undo`, `pipelines.redo` |
| run-control | `packages/run-control/contracts.ts` | `runs.observe`, `runs.attempts`, `runs.artifacts`, `runs.preflight`, `runs.start`, `runs.list`, `runs.get`, `runs.events`, `runs.preview_change`, `runs.apply_change`, `runs.stop`, `runs.resume` |
| server-control | `packages/server-control/contracts.ts` | `servers.list`, `servers.status`, `servers.resolve`, `servers.events`, `servers.register`, `servers.update`, `servers.archive` |

- 六个 HTTP 命令组：/studio-pipelines、/studio-control（服务器）、/studio-runs、/studio-builds、/studio-catalog、/studio-monitoring，各提供 v1/session 和 v1/commands；/studio-commands/v1/session 聚合实际权限目录。
- /studio-mcp：Streamable HTTP（当前安装SDK最高2025-11-25）；stdio通过RemoteControl连接同一控制服务。/studio-mcp/v1/info 读取能力配置；/studio-assistant/v1/turn 提议工具调用，不直接执行业务。
- MCP resources：cyrene://context、cyrene://templates/local-diagnostic；prompt：workflow-assistant。源码 apps/mcp/server.ts；外部OAuth、新协议和第三方MCP server聚合尚未实现。
- /studio-runs/v1/stream 是真实运行观测；/studio-events/v1/stream 是已保存图版本变化。它们不是通用日志收集接口。
- /studio-team/v1 的 session/login/logout/members/tokens/tokens/revoke 属身份管理；不自动变成模型工具。业务权限覆盖 pipelines/runs/servers/builds/catalog/products，workspace 另行校验。
- /studio-catalog/v1/packages、/reload 是现有目录服务；其与 catalog.activate 的受信构建激活路径需分别核对。build profile/凭据/仓库与镜像 allowlist 来自受信配置。
- /api/v1/* 与 /api/proxy/* 的实际可转发范围以 tooling/product-proxy.ts 和 tooling/settings-proxy.ts 为准，不是任意Product API通道。现有业务代理使用独立Web Host认证；授权白名单不等于相应接口已自动成为MCP工具。
- ExecutionAdapter、ServerObserver、BuildAdapter、CommandExecutor、StateStore/StoreFactory 的源码入口在 JSON 的 typescript_interface 类别；具体签名以源文件为准。未注入observer/adapter不能显示已连接或可执行。

## Plugins 能力目录

以下是 contracts/capabilities.yaml 在该快照列出的能力。证据等级及 REAL/SIMULATED/MOCK 保留在机器索引 declaration.implementations，未在本次盘点中重验。

| capability | 契约方法 | 权威形态 |
| --- | --- | --- |
| `agent.runtime.v1` | run, run_stream | proto |
| `compatibility.evaluator.v1` | evaluate | owner-scoped |
| `computer.runtime.v1` | create_artifact, execute_command, execute_command_stream, get_artifact, list_dir, read_file, write_file | proto |
| `dataset.preparation.v1` | inspect, prepare, transform | owner-scoped |
| `evaluation.runner.v1` | evaluate | owner-scoped |
| `execution.engine.v1` | diagnostics, import_model, inspect, start, stop | owner-scoped |
| `memory.provider.v1` | delete, export_batch, get, import_batch, prune, recall, store | proto |
| `message.connector.v1` | inbound_message, inbound_request, respond_request, send_message | proto |
| `model.analyzer.v1` | analyze, compute_sha256, validate_shards | owner-scoped |
| `model.provider.v1` | chat_completion, chat_completion_v2, embeddings | proto |
| `qq.client.v1` | 79项，分账号/登录/联系人/会话/消息/媒体等；逐项见机器索引 | owner-scoped |
| `tool.dataset.validator.v1` | validate | owner-scoped |
| `tool.provider.v1` | call_tool, list_tools | proto |
| `training.llama-factory.v1` | compile, inspect, parse_event | owner-scoped |

DirectPluginRuntime 另外提供 Invoke / InvokeStream / Health。能力 proto 多为消息契约，不是每种能力都有独立gRPC service。扫描到15份 manifest，其中14份位于当前目录发现范围，另1份为OneBot的rollback/python-0.2.0回滚版本（catalog_candidate=false）。仓库自身manifest门禁验证14份通过，capability目录检查14类能力/21条实现记录通过；这只是schema/目录一致性验证。插件与14类 capability 不是一一对应；同能力可以有多个实现，一个插件也能支持多个方法/能力。

公开 C ABI、生成语言投影与TCK入口都在Plugins contracts/ 与 sdk/；不要把 generated bindings 作为另一份手工维护的权威。capability-verification.json 明确部分 runtime 只达到模拟分发验证，合同存在不代表所有方法已dispatch或可在生产调用。

## Platform接口族

以 contracts/proto 下 canonical 版本为入口，contracts/rust/cy-proto/proto 含镜像副本。v1/v2同名service仍是不同协议，需先协商实际版本。

| 接口族 | 代表性操作 / 职责 |
| --- | --- |
| KernelService | GetKernelCapabilities、AcquireLease/ReleaseLease、LaunchProcess/TerminateProcess、Get/Cancel/WatchOperation |
| KernelAuthorityService v1/v2 | Negotiate、Acquire/Renew/ReleaseLease、Start/StopWorker、ReportHeartbeat、Create/Report/CancelOperation、Publish/Authorize/RevokeEndpoint、Read/WatchEvents |
| PluginLifecycleService | Install/UninstallPlugin、SetPluginEnabled、Start/StopPlugin、Get/ListPluginInstances、ReportPluginHeartbeat、ConnectWorker、WatchPluginEvents |
| ServiceSupervisionService | StartService、GetServiceStatus、StopService、CancelService、WatchServiceEvents |
| KernelProviderService | RegisterProvider、PublishInventory、ReconcileProvider |
| Node/WorkerControlService | Connect 控制流；身份、代次与会话不能由普通节点JSON指定 |
| WorkspaceDirect/Relay | Execute / Connect；按Workspace Fabric边界解析授权 |
| 类型化消息与Rust ports | hardware/sandbox/semantic/resource、Artifact schemas及状态/流/执行适配trait |

这些是源码契约索引，不是建议为每个RPC直接创建MCP工具。安装/准入/Lease/恢复要由既有owner流程维护，不能让AI跳过Platform边界操作进程或Docker。

## Product接口导航

| Product | 主要对象/动作 | 提供方源码入口 |
| --- | --- | --- |
| Catalyst | Dataset/Version、Preparation导入/映射/划分/确认/发布、样本/导出、Feedback导入、交接Yield | src/cyrene_catalyst/api.py |
| Yield | TrainingDraft、配置导入导出、Run预检/启动/观察/事件/诊断/取消/恢复、Result与Reactor/Exchange交接、Studio execution attempts | training/core/src/cy_exec/training/product_api.py |
| Echo | EvaluationSuite/Input/Run、Feedback/HumanAnnotation、样本/诊断及业务交接、Studio execution attempts | src/cyrene_echo/api.py（以机器索引列出的实际文件为准） |
| Reactor | DeploymentDraft/Deployment、部署/停止/重启/事件/诊断、Endpoint、ServingBinding、ModelImport、导出和Exchange接收 | product/src/cyrene_reactor_product/api.py |
| Exchange | GatewayEndpoint/Route、APIKey、route draft确认、UsageAudit、模型目录及chat completions、同源入口/代理 | product/src/cyrene_exchange_product/api.py、audit_api.py、server.py |
| Navigator | WorkspaceSnapshot、Harness Session/Handle/Event/Append/Flush/Heartbeat/Release/发送Echo、WebHost配对/会话/凭据/代理 | src/cyrene_navigator/api.py、persistence/api.py、web_host.py |

每仓的 contracts/product/v1/*openapi.yaml 是声明入口；当前观察到的声明操作数与源码路由数不同，不能因此直接判定API有错（可能含health、内部执行、代理或跨产品副本），但实际接入须逐项比对schema、router挂载、鉴权及调用方。Navigator persistence 使用路径常量拼接；扫描器已解析该形式，不能因为没有路径字面量装饰器就判定接口不存在。

## 已发现的文档/能力缺口

1. Workspace:docs/PLUGINS.md 示例把 capabilities 写为 provides/requires 对象、runtime写字符串并省略kind，与Plugins真实schema不符。按真实schema与有效插件清单开发；本次未修改旧Workspace文档。
2. 工作台节点包与官方PluginManifest是不同schema；目前没有通用 node/view/logSource/MCP贡献清单和完整第三方页面加载器。用户认可扩展方向，不等于插件可直接写入这些字段生效。
3. Navigator当前支持流程控制事件和提供方反馈；统一平台日志源、检索/订阅、上下文贡献注册及插件卸载清理需要新增宿主能力。
4. Product业务管理接口还没有全部投影成Client MCP；Plugins tool.provider.v1也不会自动出现在Client tools/list。新功能需实际注册和授权适配。
5. Platform/Plugins的logging-and-errors同步副本标为REVIEW_READY，声明规范源在Workspace；本次Workspace checkout中该源路径缺失。保留版本差异事实，不静默编辑镜像或宣布已全面采纳。
6. 源码能力、已配置provider、已部署服务和真实GPU/云验收分开；本次未验证远端或外部服务。

## 更新索引

使用 [inventory_interfaces.py](../scripts/inventory_interfaces.py)，Python与PyYAML，逐仓传入本机已确认路径：

```text
python -X utf8 inventory_interfaces.py --repo Client=/checkout/client --repo Plugins=/checkout/plugins --repo Platform=/checkout/platform --output interface-inventory.json
```

还需分别传入Workspace、六个Product，才能重建完整覆盖。脚本只读Git当前工作树和源码，不导入业务模块、不安装依赖、不fetch、不启动服务。输出路径由调用方显式给定；更新前确认不会覆盖用户文件。HEAD/status并非跨仓原子快照，团队并发编辑时需重新核对相关源摘要。

脚本索引支持Python静态路由和常量拼接、OpenAPI、proto、Client命令及HTTP入口、MCP资源/提示、manifest/schema、Rust/TS接口及C ABI头；动态挂载和特性条件需人工复核。新注册机制出现时扩展扫描规则或补相应owner索引，不能用零解析错误证明全系统接口穷尽。
