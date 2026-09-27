# 功能覆盖、回归和交付

按实际变更选择检查；不是每次改文案都要求全仓审计。开始前标注每项为适用/不适用/待宿主实现，不要求每个插件都有节点或页面。

| 维度 | 开发说明应回答 |
| --- | --- |
| 能力与所有者 | 解决什么问题？谁拥有业务结果？已有哪个 capability/interfaceVersion 可复用？ |
| 发现与注册 | manifest、SDK、dispatcher、Product绑定、节点包、MCP tools/list、监控来源是否都实际注册？ |
| 输入输出与错误 | 类型/上限/默认值/流终止是什么？稳定错误和未知结果如何表达？ |
| 节点与排版 | 是否适合流程节点？端口、参数、版本、资源引用、执行 adapter 与 pinned 布局如何保持？ |
| MCP | AI 如何发现、操作、读取结果、处理冲突？哪些内部方法明确不暴露？ |
| Navigator / 日志 | 按哪个上下文出现？标准视图还是专用视图？来源/游标/重连/卸载由谁管理？ |
| 身份与配置 | workspace/actor/scopes 与服务凭据如何传递？哪类参数可热更？secret ref 谁解析？ |
| 状态与制品 | 存储和迁移由谁拥有？大文件引用如何校验？卸载保留什么？ |
| 执行与资源 | task/reference/service 如何选？attempt 幂等、停止、重启核对、Lease/Fence、输出提交有哪些保证？ |
| 依赖和兼容性 | 多版本并存、能力绑定、降级、schema 迁移及老文档/运行快照如何处理？ |
| 发布和证据 | 支持的语言/OS、SDK/镜像摘要、许可证、清单、TCK、实际联调和回滚证据是什么？ |

## 最小但有意义的验证

- 契约：实际 manifest/schema、正反输入、版本拒绝、stream payload/error/end、能力绑定失效；不能仅测试 JSON 里有某个字段。
- 授权：错误 workspace、缺少 scope、撤销/过期凭据必须在真实副作用之前失败；模型提议不执行，批准后的 UI/MCP 使用同一 owner 服务。
- 写入：同键重放不重复任务、不同内容冲突、版本冲突保留新编辑；响应丢失后核对原 attempt，不重复启动/发布/发消息。
- 生命周期：关闭页面不虚构停止；插件断开/控制重启/停用/升级不丢失已承诺的状态；终态与制品身份正确，清理不误删他人资源。
- UI/观测：实际节点过滤、全局来源可达、工作空间切换、迟到回包、停靠展开状态保持、重复事件/游标过期/日志量上限、中英切换。
- 真协议：通过 MCP 客户端 initialize、tools/list、tools/call 验证目录和业务后果；Product→Platform授权→Plugin直接协议按目标 runtime 的 TCK 或集成夹具验证。

## 现有入口

Plugins 以 .github/workflows/public-ci.yml 为门禁权威。当前有 tools/ci/validate_manifests.py --root .、tools/ci/capability_catalog.py --root . --check、tools/ci/verify_source_manifest.py --root . --allow-git-metadata、tools/ci/check_public_repository.py。语言/TCK命令按该 checkout 的 CONTRIBUTING、对应 pyproject/Cargo/csproj/pom 与 CI 选择，不照 Workspace 旧语言基线盲目升级。

公开 Plugins 仓库采用 source-manifest 记录受审导出文件摘要。修改包后遵循其 clean export 流程，不能为了验证“通过”直接重写摘要或移植私有 Git 历史。保护协议研究文档与许可证边界；第三方许可证、依赖/SBOM、包镜像来源与兼容声明随实际发布流程维护。

Client 可运行 npm test、npm run build、npm run test:e2e、npm run test:e2e:control；定向入口有 node-registry、run-control、mcp-platform、assistant、monitoring 单元与 control-e2e/mcp.spec.ts。本地诊断验证调度/观察，不验证 GPU 或独立容器；模型 fixture 不证明供应商可用。

## 交付措辞与持续维护

分别报告本地实现、源码测试、外部联调、提交、远端 CI 与部署。capability-verification.json 的 DECLARED/其它 level 及 REAL/SIMULATED/MOCK 是既有证据主张，不因发现实现文件而自动上调。

接口快照包含 HEAD 和工作树状态；有未提交改动时，HEAD 不是这些接口的完整可复现版本，应结合 source_hashes。回归结果记录实际命令和环境；未运行/跳过/模拟/凭据受阻分别保留。

重大改动把旧缺口改为有来源的实现说明，更新本 skill 对应参考与必要接口快照；不无限追加日志。已有 UI 专项约定仍可由 cyrene-studio-development 维护，通用插件权责以本 skill 及实际 owner 契约为准。skill 无法监视未来提交，更新发生在后续相关开发任务中。
