# 节点、MCP、Navigator 与日志贡献

## 已有与拟议能力分开

2026-09-27 Client 实现：声明式节点 packageSchema、版本化节点目录/激活，图操作，运行/构建/服务器服务，HTTP/stdio MCP，模型工具提议/确认，Navigator 运行监控及页面停靠/展开。第三方可执行页面宿主、通用监控贡献注册、统一平台日志源注册、完整插件启停到页面注销的联动尚未实现。

下文“监控视图”“日志来源”“宿主贡献注册”是后续设计要求，不是现成可 import 的 SDK。node/view/command/provider/logSource 只是分类词，不是已冻结的 manifest 字段；不要直接写进现有 PluginManifest 并声称支持。新增宿主 schema、加载器与权限适配需要在相应 owner 内实现后再更新状态。

## 节点贡献

Client:packages/node-registry/contracts.ts 使用 schemaVersion=cyrene.studio.nodes.v1、id/version、可选 pluginRef 和 nodes。每个节点提供 type/version、title/owner/category/description/color、inputs/outputs、fields/defaults、execution。配置当前以字符串/数字为值，字段类型 text/number/select；端口 kind 当前为 dataset/model/compute/evaluation/endpoint/agent-report。不要假设支持任意 JSON 参数或任意端口类型。

execution.kind 支持 reference/task/service/external；adapter 是控制服务注册名，image 如提供必须固定摘要。声明 checkpoint/mutableFields 后，adapter 必须确有能力。pluginRef 只关联身份，不负责自动安装官方插件或打开页面。

明确执行由哪个 Product 管理；让节点可查看绑定、配置、预检、运行与错误。引用资源和启动任务分开，服务端业务图不用 LiteGraph ID。新增节点默认位置可由 layout 计算；尊重 pinned 与局部排版，不能为监控排版改写运行快照。版本升级保留未知类型/端口快照和用户未保存草稿。

## MCP 投影

现有 Client:apps/mcp/server.ts 从六组命令注册工具：pipeline-control（含 nodes）、run-control、build-control、server-control、node-registry、monitoring。apps/control/application.ts 提供同服务 HTTP，权限来自真实 actor；write tool 要幂等键。新能力应先落 owner 应用命令，然后同步工具发现和 UI 入口。不能只加一个 MCP wrapper 绕过业务验证。

工具至少交代：名称/用途、完整输入输出、workspace 范围、只读/副作用、版本要求、幂等与恢复、查询途径和错误。annotations 是提示而不是授权；UI 按钮隐藏也不是安全边界。所有执行入口重新校验权限；模型返回、插件描述、日志内容属于数据，不自动升级成系统指令。

resource 适合授权后的只读上下文、能力说明和模板；prompt 适合组合任务指导，不应将未授权信息嵌入描述。大日志按游标/分页查询，不在 resources 或工具响应中整库导出。MCP 调用记录与平台长期审计不是同一存储能力。

当前 HTTP/stdio 使用安装 SDK 支持的最高 2025-11-25 协议；不声明 OAuth、2026-07-28 协议或第三方 MCP 聚合已完成。嵌入助手只提议调用，由用户检查后通过真实 MCP 执行；外部 AI 客户端自行承担其交互批准策略，服务端权限/版本/幂等仍一致。

如果能力是纯视觉折叠、主题或局部滚动，不必变成 MCP tool。凭据签发、宿主内部恢复、任意 shell 等也不自动暴露。该判断应写入功能说明，不能用“已留接口”替代实际可发现/可调用/可观察的验收。

## Navigator 扩展设计

Navigator 在这里指 Client 的运行监控窗口，不是 Cyrene-Navigator 的 Agent/Harness Product。建议以后插件提供可选监控贡献：上下文选择条件、摘要/详情数据、视图注册元数据，以及经权限校验的操作。业务数据来自 owner 接口，不在 renderer 重做任务状态机。

节点贡献在当前 workspace/选定 pipeline 实际存在时出现；多个同类型节点保留各自实例、run/attempt 区分；正在运行而从草稿移除的节点仍需可观察。全局平台/服务器贡献可独立出现，不能一律套“无节点就隐藏”。工作空间切换、固定查看和历史过滤都必须限定真实身份范围。

优先使用宿主标准摘要/日志视图，专用曲线/报告页面是可选贡献。右侧停靠和主区域展开复用视图状态及订阅；常用信息与高级执行细节分离，中英文文案与键盘/可访问性一起接入。插件异常应局部降级，不让一个 renderer 弄坏工作台。

未来执行第三方页面代码前明确信任/隔离边界、宿主 SDK、CSP、依赖版本、权限与注销接口。当前不通过下载并 eval 任意 JS、读取本地路径或直接开放数据库来“快速支持插件”。这一点不禁止受信内置组件使用现有 React 实现。

## 日志来源设计

用户要求平台实时日志可扩展。数据来源和 renderer 解耦；插件只提供符合契约的数据时，应能复用统一日志视图。设计至少覆盖以下行为，再确定公开字段/工具名称：

- 发现当前身份可读的来源，并说明服务/插件/服务器/节点关联；注册由受信宿主完成，不允许浏览器指定任意文件或远程 URL。
- 有界历史查询与实时观察，携带稳定游标/序号；明确重连、重复、缺口、过期、乱序与背压处理。暂停滚动不等于无限制缓存；历史过期应提示。
- 时间、级别、来源、事件/错误标识、消息及可用的 trace/request/workspace/pipeline/run/node/attempt 关联。不是每条平台日志都有节点，禁止用虚假 ID 强行关联。
- 授权在服务端执行，脱敏在离开数据源前完成；日志保留/采样/导出策略与安全审计单独声明。插件日志文本不驱动成功/失败终态，第三方训练输出由所属 adapter 转为类型化事件。
- 统一列表/查询能力投影成 MCP tools；实时能力按实际 transport 提供，不能把普通 watch 冒称 durable subscription。工具名/schema 由正式实现决定，此处不发布虚构 logs.* API。
- 插件禁用/卸载注销新来源与活动订阅；历史记录保留来源及版本，按保留策略可读。单个来源断开不停止其它来源，也不将缓存的最后一条记录标作仍然实时。

当前可复用 runs.observe/runs.events、/studio-runs/v1/stream 及共享前端观测。它们是流程控制/提供方反馈事件，UI 缓存最近200条；不是完整宿主 stdout、容器日志或通用日志聚合。底栏 events 是浏览器工作台记录，也不是后端日志库。

Plugins/Platform 的 docs/logging-and-errors.md 同步副本标为 REVIEW_READY 草案，指向 Workspace 的规范源；本次 Workspace checkout 未发现该源文件。新接入先核实被接受版本，不直接编辑同步副本或默默把草案全部升格为已部署协议。
