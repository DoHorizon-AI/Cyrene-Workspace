# Cyrene Teams Assistant Privacy Notice

Last updated: 2026-10-24

This notice describes the internal Cyrene assistant when it is installed in a Microsoft Teams team and the required permissions and services are enabled. The teams and channels in scope are determined by the installation and service configuration.

## Information the assistant processes

The Teams integration can receive channel messages for an installed team when the app is granted the Teams resource-specific consent permission `ChannelMessage.Read.Group`. To route a message to a thread session, associate replies with the original post, and avoid processing a delivery twice, the integration processes message text and stable tenant, team, channel, thread, message, and actor identifiers. Messages from other channel participants may be included as context in a thread.

Only a configured maintainer can start an assistant task or authorize a tool action. A participant's message may be present as conversation context; it does not grant that participant the maintainer's permissions or cause an action to run as that participant.

## How information is used and stored

The assistant uses message text and thread context to respond and, when requested by an authorized maintainer, to carry out an allowed task. A request that needs a model is sent through the configured CodeBuddy provider, such as the configured GLM or DeepSeek model. This notice does not make a claim about a model provider's retention or training practices; those depend on the provider and the service configuration in use.

Navigator stores durable task and session records and an outbound-message queue. Runtime logs are also kept in storage controlled by the service operator. These records survive a service restart. This notice does not specify a retention period or provide a user-facing deletion control. Contact the service operator for questions about operational retention or deletion.

Application credentials and connector credentials are held by the service. Do not paste passwords, application secrets, access or refresh tokens, API keys, or sign-in codes into Teams messages.

This notice describes the documented message and task path. It does not make additional promises about telemetry or upstream data handling beyond the points above. Actual features and data flows depend on the deployed service configuration.

## Contact

Contact the Cyrene repository maintainers through the [repository issues](https://github.com/DoHorizon-AI/Cyrene-Workspace/issues) when enabled, or follow the [contribution guide](../CONTRIBUTING.md).

---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene Teams 助手隐私说明

更新日期：2026-10-24

本说明适用于 Cyrene 助手安装到 Microsoft Teams 团队，并启用所需权限和服务时的内部使用。实际涵盖的团队和频道由应用安装范围及服务配置决定。

## 助手处理的信息

应用获得 Teams 资源特定许可 `ChannelMessage.Read.Group` 后，可以接收已安装团队中的频道消息。为了将消息路由到线程会话、把回复关联到原帖并避免重复处理，集成会处理消息文本，以及稳定的租户、团队、频道、线程、消息和发言者标识符。同一频道线程中其他参与者的消息也可能作为对话上下文处理。

只有已配置的维护者可以启动助手任务或授权工具操作。其他参与者的消息可以出现在对话上下文中，但不会因此获得维护者权限，也不会让操作以该参与者的身份执行。

## 信息用途与存储

助手使用消息文本和线程上下文生成回复；已授权的维护者提出任务时，助手也会据此执行许可范围内的操作。需要模型处理的请求会发送到服务配置的 CodeBuddy 模型提供方，例如配置中的 GLM 或 DeepSeek。本说明不声明模型提供方如何保留或训练数据；具体做法取决于所使用的提供方和服务配置。

Navigator 会保存持久任务和会话记录，并保存待发送消息队列。运行日志也保存在服务运营者控制的存储中。这些记录不会因服务重启而删除。本说明未规定保留期限，也未提供面向用户的自助删除入口。有关运行记录的保留或删除，请联系服务运营者。

应用凭据和连接器凭据由服务端保存。请勿在 Teams 消息中粘贴密码、应用密钥、访问或刷新令牌、API 密钥或登录代码。

本说明描述已记录的消息与任务处理路径，不对上述内容以外的遥测或上游数据处理作额外承诺。实际功能和数据流取决于已部署的服务配置。

## 联系方式

在仓库已启用 Issues 时，可通过[仓库 Issues](https://github.com/DoHorizon-AI/Cyrene-Workspace/issues)联系 Cyrene 维护者；也可参阅[贡献指南](../CONTRIBUTING.md)。
