# Cyrene Teams Assistant Usage

This page describes intended internal use of the Cyrene assistant in Microsoft Teams. Availability and allowed operations depend on the installed app, configured maintainers, connector grants, and service configuration. This page is operational usage guidance, not a separate user agreement. See the [Cyrene Navigator LICENSE](https://github.com/DoHorizon-AI/Cyrene-Navigator/blob/develop/LICENSE) for the Navigator component's software license.

## Starting and continuing a task

In a team channel where the app is installed, a configured maintainer starts a task by mentioning `@Cyrene`. Continue the conversation by replying in the same thread; the service associates the team, channel, and root post with the task session. Other participants' replies can provide context, but only configured maintainers can start tasks or authorize tool actions.

Give the assistant a clear goal and identify the relevant repository, resource, or environment. When a required target or parameter is missing, the assistant should ask for it. Explicit instructions can run an operation that is allowed by the configured connector identity and scope without another per-action confirmation.

## Connector actions and credentials

Connector actions use the credentials and permissions configured on the service. A Teams message does not grant provider access or transfer the sender's identity to a connector. Results depend on the permissions granted upstream; the assistant should report permission and provider errors plainly. For a long-running operation, use its returned status or operation reference to check progress. If the result is uncertain, check its status before asking the assistant to retry an operation that could create or change a resource.

Do not send passwords, application secrets, access or refresh tokens, API keys, or sign-in codes in Teams. Use the service's private configuration process for credentials. Requests sent to a configured model provider may include the relevant prompt and conversation context; see the [privacy notice](TEAMS_CHAT_PRIVACY.md) and the applicable provider settings.

## Help and issues

For access or behavior questions, contact the Cyrene repository maintainers through the [repository issues](https://github.com/DoHorizon-AI/Cyrene-Workspace/issues) when enabled, or follow the [contribution guide](../CONTRIBUTING.md).

---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene Teams 助手使用说明

本页说明 Cyrene 助手在 Microsoft Teams 中的预期内部用法。能否使用以及允许哪些操作，取决于已安装的应用、配置的维护者、连接器授权和服务配置。本页是操作使用说明，不是单独的用户协议。Navigator 组件的软件许可见 [Cyrene Navigator LICENSE](https://github.com/DoHorizon-AI/Cyrene-Navigator/blob/develop/LICENSE)。

## 启动和继续任务

在已安装应用的团队频道中，配置的维护者通过提及 `@Cyrene` 启动任务。在同一线程中回复即可继续对话；服务会将团队、频道和根帖子关联到任务会话。其他参与者的回复可以提供上下文，但只有配置的维护者可以启动任务或授权工具操作。

请说明目标，并指出相关仓库、资源或环境。缺少必要目标或参数时，助手应当询问。明确指令可以直接执行配置的连接器身份及权限范围所允许的操作，无需对每项操作再次确认。

## 连接器操作与凭据

连接器操作使用服务中配置的凭据和权限。Teams 消息本身不会授予供应商访问权限，也不会把发言者身份转交给连接器。操作结果取决于上游授予的权限；助手应如实说明权限不足和供应商错误。长时间运行的操作会提供状态或操作引用，可据此查看进度。如果操作结果不确定，请先查询状态，再要求助手重试可能创建或更改资源的操作。

请勿在 Teams 中发送密码、应用密钥、访问或刷新令牌、API 密钥或登录代码。凭据应通过服务的私有配置流程提供。发送给已配置模型提供方的请求可能包含相关提示词和对话上下文；详情请参阅[隐私说明](TEAMS_CHAT_PRIVACY.md)及适用的供应商设置。

## 帮助与问题反馈

如需询问访问权限或使用问题，在仓库已启用 Issues 时，可通过[仓库 Issues](https://github.com/DoHorizon-AI/Cyrene-Workspace/issues)联系 Cyrene 维护者；也可参阅[贡献指南](../CONTRIBUTING.md)。
