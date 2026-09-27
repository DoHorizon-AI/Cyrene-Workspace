# 插件契约、运行与数据边界

## Manifest 事实

权威为 Plugins:manifests/plugin.manifest.schema.json 和实际 plugins/**/plugin.manifest.json。2026-09-27 读取的 schema 要求 schemaVersion=1、id/name/version/kind/capabilities/runtime。capabilities 是字符串数组；runtime 是包含 language 的对象，不是单个语言字符串。methods 可声明 capability、interfaceVersion、executionMode、inputSchema/outputSchema。多 capability 插件逐方法核对选中的能力。

kind 当前枚举：capability-plugin、gateway-runtime、provider-adapter、tool-provider、system-plugin。执行模式 inline/worker/service 由 Platform 解释，不自行新增 cloud/container/node 等模式字符串。是否启动进程、容器或复用服务，取决于宿主和部署实现。

runtime.protocol 声明版本化数据协议；runtime.launch.executable/args 是部署声明，不允许模型随意指定命令。Python runtime 可使用 prepared-runtime + cyrene_plugin_runtime/bootstrap.py；具体 entrypoint、依赖准备、配置入口参照现有插件和 SDK，不复制不适用的启动模板。支持语言以实际 schema 和 runtime 实现为准。

configuration.schemaRef/defaultSettings、secrets.requiredSecretRefs/optionalSecretRefs、state.isStateful/storageCategory/migrationSupported/downgradeSupported/retentionPolicyOnUninstall 均已有 schema 位置。字段存在不等于每种 runtime 都消费它，需跟到 bootstrap/configuration 和安装器。

distribution 如使用，必须满足当前 $defs/connectorPackageReference，包括 specVersion、descriptorRef、publicationStatus；不要抄旧 Workspace 示例中的 STABLE。兼容范围声明 platformVersion、语言和 OS，运行镜像/SDK版本以实际构建锁和 CI 为准。

## 调用路径

Product 通过 Platform 得到授权的 opaque connection reference，再按 Plugins 的能力契约直连对应实例。Platform 不解析/分发业务 payload，不成为统一 Plugin Invoke 网关。

Plugins:contracts/proto/cyrene/plugin/runtime/v1/direct_plugin_runtime.proto 定义 DirectPluginRuntime 的 Invoke、InvokeStream、Health。调用携带 capability、interface_version、method、payload_type_url、payload、request_id、stream_mode；stream 包含 payload/error/end。能力 proto 可能只定义消息而不提供独立 gRPC service，因此不能给每个 capability 凭空拼接 RPC URL。

客户端 SDK 和 plugin host 的职责不同。按语言读取 Plugins:sdk/python/cyrene_plugin_runtime（server/client/bootstrap/configuration/errors/logging）、sdk/java 或 runtime 下实际实现。MCP tools/list/call 与 DirectPluginRuntime 不是可以互换的 wire format；协议之间通过 owner 应用服务适配。

## 配置、密钥和状态

分别说明插件安装级配置、工作空间配置、节点参数、运行快照和用户偏好。声明默认值、合法范围、是否必填、哪些修改需新运行或重启。不要把任务参数直接当宿主进程环境变量。

密钥以 secret reference 留在受信配置边界；查询接口返回引用/状态，不返回明文。Product会话、Studio会话、MCP API token、云凭据和插件实例身份不混用。重载后的轮换和失效应可验证；不要让前端将任意 URL 当日志源或执行端点。

选择状态存储类别并说明拥有者、事务/并发、备份、迁移和卸载保留。插件删除不默认清除训练制品、用户对话或已发布数据。控制数据库的运行/幂等回执与插件临时缓存、日志轮转分离；不要从浏览器直接改业务数据库。

## 任务、服务与恢复

Client RunControl 的 adapter 契约位于 packages/run-control/adapter.ts：preflight、start、lookup、stop、change。Assignment 固定 workspace/run/node/attempt/generation、inputs、placement、image、targets/checkpoint；Capabilities 明确在线可变字段、checkpoint 和 safeRetry。尚未实现的能力应拒绝，不返回空的“成功”。

start 在产生外部副作用前持久化 attempt 去重；响应丢失查原 attempt，只有权威 absent 才允许按既有状态机推进。stop 的接受不等于执行终止；终态、输出与 generation/attempt 关联一致后才释放资源或恢复。修改 desired config 不表示实际生效。Product 内重试与跨步骤恢复不能相互叠加。

PluginManifest 的 worker 不等于每个画布节点要开一个容器。已接受的目标是任务尝试隔离执行，引用节点不启动容器、长期服务单独管理；当前各 owner 的容器/监管装配状态须实证。Client 本地诊断是进程内 CPU 工作，不能作为分容器或 GPU 已接通的证据。

资源与制品按 Platform 的 NodeRef/epoch、Lease/Fence 和 ArtifactRef/manifest digest 语义走现有实现。插件不自行伪造 ONLINE、抢租约或替 Product 宣告业务 READY。大文件和模型通过制品引用/流式下载，不塞入节点配置、MCP 响应或日志。

## 版本与热更新

区分 plugin release version、capability interfaceVersion、node typeVersion、graph/layout revision、execution generation、package/image digest。变更 schema 或端口时评估老文档和已运行快照；Client 已发布节点包/类型不可变，旧版本保留。

热加载目录、启动新版本实例、迁移持久状态、接管存活任务是不同能力。插件停用时阻止新调用并明确在途任务策略；页面卸载关闭监听器，业务任务不因关面板自动取消。升级失败可以回到已核验版本，但不可将不可逆数据迁移假称可回滚。
